"""MongoDB registry; run the thread-safe PyMongo driver outside the asyncio loop."""

import asyncio
import os
from functools import wraps

from fastapi import HTTPException
from pydantic import ValidationError
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from orchestrator.documents import (
    AgentDocument,
    BindingDocument,
    BindingUpdate,
)


def database_operation(function):
    @wraps(function)
    async def execute(*args, **kwargs):
        operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Threads cannot be cancelled. Keep the caller's lock until this write
            # finishes, so a cancelled request cannot mutate a session after cleanup.
            await asyncio.gather(operation, return_exceptions=True)
            raise
        except PyMongoError:
            # Driver exceptions can include connection details. Never return/log them.
            raise HTTPException(503, "MongoDB registry unavailable") from None

    return execute


class MongoRegistry:
    def __init__(self, client=None, database=None):
        self.owns_client = client is None
        if client is None:
            uri = os.getenv("MONGODB_URI", "")
            if not uri:
                raise ValueError("MONGODB_URI is required")
            try:
                timeout = int(os.getenv("MONGODB_TIMEOUT_MS", "5000"))
                if timeout <= 0:
                    raise ValueError
            except ValueError:
                raise ValueError("MONGODB_TIMEOUT_MS must be a positive integer") from None
            options = {
                "serverSelectionTimeoutMS": timeout,
                "connectTimeoutMS": timeout,
                "socketTimeoutMS": timeout,
                "appname": "pi-session-manager",
            }
            username, password = (
                os.getenv("MONGODB_USERNAME", ""),
                os.getenv("MONGODB_PASSWORD", ""),
            )
            if bool(username) != bool(password):
                raise ValueError("Set both MONGODB_USERNAME and MONGODB_PASSWORD")
            if username:
                options.update(
                    username=username,
                    password=password,
                    authSource=os.getenv("MONGODB_AUTH_SOURCE", "admin"),
                )
            if os.getenv("MONGODB_TLS_CA_FILE"):
                options.update(tls=True, tlsCAFile=os.environ["MONGODB_TLS_CA_FILE"])
            try:
                client = MongoClient(uri, **options)
            except (PyMongoError, ValueError, OSError):
                raise ValueError("Invalid MongoDB connection configuration") from None
        self.client = client
        self.db = client[database or os.getenv("MONGODB_DATABASE", "pi_agents")]

    @database_operation
    def initialize(self):
        self.client.admin.command("ping")
        self.db.bindings.create_index([("agent_id", 1), ("created_at", 1), ("_id", 1)])
        self.db.bindings.create_index("sandbox_id")
        self.db.bindings.create_index("status")
        # NOT a TTL index: worker deletion must succeed before removing a binding.
        self.db.bindings.create_index([("retention", 1), ("expires_at", 1)])

    @database_operation
    def ping(self):
        self.client.admin.command("ping")

    @database_operation
    def get_agent(self, agent_id):
        row = self.db.agents.find_one({"_id": agent_id})
        return self.load(AgentDocument, row).to_mongo() if row is not None else None

    @database_operation
    def insert_agent(self, agent_id):
        self.db.agents.insert_one(AgentDocument(id=agent_id).to_mongo())

    @staticmethod
    def load(model, document):
        try:
            return model.model_validate(document)
        except ValidationError:
            raise HTTPException(503, "Invalid MongoDB registry document") from None

    @classmethod
    def public(cls, document):
        return cls.load(BindingDocument, document).public() if document is not None else None

    @database_operation
    def sessions(self, agent_id):
        return [
            self.public(row)
            for row in self.db.bindings.find({"agent_id": agent_id}).sort(
                [("created_at", 1), ("_id", 1)]
            )
        ]

    @database_operation
    def binding(self, session_id, agent_id=None):
        query = {"_id": session_id}
        if agent_id is not None:
            query["agent_id"] = agent_id
        return self.public(self.db.bindings.find_one(query))

    @database_operation
    def counts(self):
        return {
            row["_id"]: row["count"]
            for row in self.db.bindings.aggregate(
                [{"$group": {"_id": "$sandbox_id", "count": {"$sum": 1}}}]
            )
        }

    @database_operation
    def insert(self, session_id, **fields):
        self.db.bindings.insert_one(BindingDocument(id=session_id, **fields).to_mongo())

    @database_operation
    def update(self, session_id, **fields):
        patch = BindingUpdate.model_validate(fields)
        row = self.db.bindings.find_one({"_id": session_id})
        if row is None:
            return
        patch.apply(self.load(BindingDocument, row))
        self.db.bindings.update_one(
            {"_id": session_id}, {"$set": patch.model_dump(exclude_unset=True)}
        )

    @database_operation
    def delete(self, session_id):
        self.db.bindings.delete_one({"_id": session_id})

    @database_operation
    def expired(self, now):
        return [
            self.public(row)
            for row in self.db.bindings.find(
                {
                    "$or": [
                        {"status": "deleting"},
                        {"retention": "ephemeral", "expires_at": {"$lte": now}},
                    ]
                }
            )
        ]

    async def close(self):
        if self.owns_client:
            await asyncio.to_thread(self.client.close)
