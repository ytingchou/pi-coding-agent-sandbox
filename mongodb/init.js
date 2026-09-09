// Runs only on a new local MongoDB volume. API credentials have database-scoped access.
const database = db.getSiblingDB(process.env.MONGODB_DATABASE || 'pi_agents');
database.createUser({
  user: process.env.MONGODB_USERNAME,
  pwd: process.env.MONGODB_PASSWORD,
  roles: [{role: 'readWrite', db: database.getName()}],
});
