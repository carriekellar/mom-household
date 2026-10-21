-- Private schema: no browser / Supabase Data API access to family information.
CREATE SCHEMA IF NOT EXISTS household;
REVOKE ALL ON SCHEMA household FROM PUBLIC;
CREATE TABLE IF NOT EXISTS household.state (
 id integer PRIMARY KEY CHECK(id=1), body jsonb NOT NULL
);
INSERT INTO household.state VALUES(1, '{"events":{},"drafts":{},"blocked":[]}') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS household.inbox (
 id bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
 sid text PRIMARY KEY, phone text NOT NULL, body text NOT NULL,
 status text NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS household.outbox (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 phone text NOT NULL, body text NOT NULL, event text, version integer,
 status text NOT NULL DEFAULT 'pending', provider_sid text, error text
);
CREATE TABLE IF NOT EXISTS household.receipts (sid text PRIMARY KEY, status text NOT NULL);
CREATE INDEX IF NOT EXISTS inbox_pending ON household.inbox(id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS outbox_pending ON household.outbox(id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS outbox_provider ON household.outbox(provider_sid);
ALTER TABLE household.state ENABLE ROW LEVEL SECURITY;
ALTER TABLE household.inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE household.outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE household.receipts ENABLE ROW LEVEL SECURITY;
