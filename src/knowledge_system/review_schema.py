"""V3.2 additive migration, executed as one atomic PostgreSQL DO statement."""

REVIEW_MIGRATION = """
DO $migration$
BEGIN
    -- Serialize concurrent init-db calls, including the constraint replacement.
    PERFORM pg_advisory_xact_lock(320032);
    ALTER TABLE proposals DROP CONSTRAINT IF EXISTS proposals_status_check;
    ALTER TABLE proposals ADD CONSTRAINT proposals_status_check
        CHECK (status IN ('pending', 'accepted', 'rejected', 'deferred'));
    ALTER TABLE proposals ADD COLUMN IF NOT EXISTS accepted_review_id uuid;

    CREATE TABLE IF NOT EXISTS proposal_reviews (
        id uuid PRIMARY KEY,
        proposal_id uuid NOT NULL REFERENCES proposals(id),
        revision integer NOT NULL CHECK (revision > 0),
        target_source_id text NOT NULL CHECK (target_source_id = 'knowledge-git'),
        target_source_path text NOT NULL,
        change_kind text NOT NULL CHECK (change_kind IN ('create', 'update')),
        base_revision text NOT NULL,
        old_content text,
        old_hash text,
        new_content text NOT NULL,
        new_hash text NOT NULL,
        diff text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (proposal_id, revision),
        UNIQUE (proposal_id, id),
        CHECK ((change_kind = 'create' AND old_content IS NULL AND old_hash IS NULL
                AND base_revision = 'absent') OR
               (change_kind = 'update' AND old_content IS NOT NULL AND old_hash IS NOT NULL
                AND base_revision = 'sha256:' || old_hash))
    );
    IF NOT EXISTS (SELECT FROM pg_constraint WHERE conname = 'proposals_accepted_review_fk'
                   AND conrelid = 'proposals'::regclass) THEN
        ALTER TABLE proposals ADD CONSTRAINT proposals_accepted_review_fk
            FOREIGN KEY (id, accepted_review_id) REFERENCES proposal_reviews(proposal_id, id);
        ALTER TABLE proposals ADD CONSTRAINT proposals_acceptance_check
            CHECK ((status = 'accepted') = (accepted_review_id IS NOT NULL));
    END IF;
    CREATE TABLE IF NOT EXISTS proposal_decisions (
        id uuid PRIMARY KEY,
        proposal_id uuid NOT NULL REFERENCES proposals(id),
        review_id uuid,
        previous_status text NOT NULL CHECK (previous_status IN ('pending', 'deferred')),
        status text NOT NULL CHECK (status IN ('accepted', 'rejected', 'deferred')),
        client_type text NOT NULL,
        external_chat_id text NOT NULL,
        external_user_id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (proposal_id, review_id) REFERENCES proposal_reviews(proposal_id, id),
        UNIQUE (proposal_id, status),
        CHECK (status != 'accepted' OR review_id IS NOT NULL)
    );
END
$migration$;
"""

# DB protection complements the frozen values and append-only store API.
REVIEW_IMMUTABILITY = """
CREATE OR REPLACE FUNCTION forbid_review_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Immutable review audit record';
END
$$;
CREATE OR REPLACE TRIGGER proposal_reviews_immutable
    BEFORE UPDATE OR DELETE ON proposal_reviews
    FOR EACH ROW EXECUTE FUNCTION forbid_review_mutation();
CREATE OR REPLACE TRIGGER proposal_decisions_immutable
    BEFORE UPDATE OR DELETE ON proposal_decisions
    FOR EACH ROW EXECUTE FUNCTION forbid_review_mutation();
CREATE OR REPLACE FUNCTION guard_terminal_proposal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('accepted', 'rejected') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'Terminal proposal is immutable';
    END IF;
    RETURN NEW;
END
$$;
CREATE OR REPLACE TRIGGER proposals_terminal
    BEFORE UPDATE ON proposals FOR EACH ROW EXECUTE FUNCTION guard_terminal_proposal();
"""

# Sent as one PostgreSQL command so autocommit still installs the migration and
# its immutability guards in one implicit transaction.
REVIEW_SCHEMA = REVIEW_MIGRATION + REVIEW_IMMUTABILITY
