"""Run registry tests against mongomock, or a disposable database on real MongoDB."""

import os
import uuid

import mongomock
import pytest
from pymongo import MongoClient

from orchestrator.registry import MongoRegistry


@pytest.fixture
def registry_factory():
    uri = os.getenv("TEST_MONGODB_URI")
    if uri:
        options = {"serverSelectionTimeoutMS": 5000}
        if os.getenv("MONGODB_USERNAME"):
            options.update(
                username=os.environ["MONGODB_USERNAME"],
                password=os.environ["MONGODB_PASSWORD"],
                authSource=os.getenv("MONGODB_AUTH_SOURCE", "admin"),
            )
        client = MongoClient(uri, **options)
    else:
        client = mongomock.MongoClient()
    databases = {}

    def factory(key):
        name = databases.setdefault(str(key), "pi_test_" + uuid.uuid4().hex)
        return MongoRegistry(client=client, database=name)

    yield factory
    for name in databases.values():
        client.drop_database(name)
    client.close()
