"""V3.3 durable apply journal and database-enforced lifecycle invariants."""

APPLY_SCHEMA = r"""
DO $migration$
BEGIN
    PERFORM pg_advisory_xact_lock(330033);

    CREATE TABLE IF NOT EXISTS proposal_applies (
        id uuid PRIMARY KEY,
        proposal_id uuid NOT NULL,
        review_id uuid NOT NULL,
        target_source_id text NOT NULL CHECK (target_source_id = 'knowledge-git'),
        target_source_path text NOT NULL,
        expected_old_hash text,
        expected_absent boolean NOT NULL,
        expected_new_hash text NOT NULL,
        apply_status text NOT NULL DEFAULT 'pending'
            CHECK (apply_status IN ('pending', 'applied', 'conflict', 'failed')),
        index_status text NOT NULL DEFAULT 'pending'
            CHECK (index_status IN ('pending', 'indexed', 'failed')),
        actual_hash text,
        apply_error_class text,
        index_error_class text,
        apply_attempts integer NOT NULL DEFAULT 0 CHECK (apply_attempts >= 0),
        index_attempts integer NOT NULL DEFAULT 0 CHECK (index_attempts >= 0),
        actor_client_type text NOT NULL,
        actor_external_chat_id text NOT NULL,
        actor_external_user_id text NOT NULL,
        intent_created_at timestamptz NOT NULL DEFAULT now(),
        last_apply_attempt_at timestamptz,
        applied_at timestamptz,
        last_index_attempt_at timestamptz,
        indexed_at timestamptz,
        refresh_proposal_id uuid REFERENCES proposals(id),
        updated_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (proposal_id, review_id)
            REFERENCES proposal_reviews(proposal_id, id),
        UNIQUE (proposal_id),
        UNIQUE (review_id),
        UNIQUE (refresh_proposal_id),
        CHECK (expected_absent = (expected_old_hash IS NULL)),
        CHECK (expected_old_hash IS NULL OR expected_old_hash ~ '^[0-9a-f]{64}$'),
        CHECK (expected_new_hash ~ '^[0-9a-f]{64}$'),
        CHECK (actual_hash IS NULL OR actual_hash ~ '^[0-9a-f]{64}$'),
        CHECK (index_status != 'indexed' OR apply_status = 'applied'),
        CHECK ((apply_status IN ('conflict', 'failed')) = (apply_error_class IS NOT NULL)),
        CHECK ((index_status = 'failed') = (index_error_class IS NOT NULL)),
        CHECK ((index_status = 'indexed' AND indexed_at IS NOT NULL)
               OR (index_status != 'indexed' AND indexed_at IS NULL)),
        CHECK (apply_status = 'pending' OR
               (apply_attempts > 0 AND last_apply_attempt_at IS NOT NULL)),
        CHECK (index_status = 'pending' OR
               (index_attempts > 0 AND last_index_attempt_at IS NOT NULL)),
        CHECK (refresh_proposal_id IS NULL OR apply_status = 'conflict')
    );

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'proposal_applies'::regclass
          AND conname = 'proposal_applies_apply_result_check'
    ) THEN
        -- Adding the validated constraint is the migration for existing V3.3
        -- schemas. Corrupt success rows abort this entire DO block; they are
        -- reported for operator repair and are never silently rewritten.
        ALTER TABLE proposal_applies
            ADD CONSTRAINT proposal_applies_apply_result_check
            CHECK (
                (apply_status = 'applied' AND actual_hash IS NOT NULL
                 AND actual_hash = expected_new_hash AND applied_at IS NOT NULL
                 AND apply_error_class IS NULL)
                OR
                (apply_status != 'applied' AND actual_hash IS NULL
                 AND applied_at IS NULL)
            );
    END IF;

    CREATE INDEX IF NOT EXISTS proposal_applies_status_idx
        ON proposal_applies (apply_status, index_status, updated_at);

    CREATE OR REPLACE FUNCTION guard_proposal_apply() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        accepted uuid;
        proposal_status text;
        review_source text;
        review_path text;
        review_old_hash text;
        review_new_hash text;
        review_kind text;
        review_count bigint;
        proposal_conversation uuid;
        refresh_conversation uuid;
    BEGIN
        EXECUTE format(
            'SELECT status, accepted_review_id, conversation_id '
            'FROM %I.proposals WHERE id = $1',
            TG_TABLE_SCHEMA
        ) INTO proposal_status, accepted, proposal_conversation USING NEW.proposal_id;
        IF proposal_status IS DISTINCT FROM 'accepted' OR accepted IS DISTINCT FROM NEW.review_id THEN
            RAISE EXCEPTION 'Apply requires the proposal accepted revision';
        END IF;
        EXECUTE format(
            'SELECT target_source_id, target_source_path, old_hash, new_hash, change_kind '
            'FROM %I.proposal_reviews WHERE proposal_id = $1 AND id = $2',
            TG_TABLE_SCHEMA
        ) INTO review_source, review_path, review_old_hash, review_new_hash, review_kind
          USING NEW.proposal_id, NEW.review_id;
        GET DIAGNOSTICS review_count = ROW_COUNT;
        IF review_count != 1
           OR NEW.target_source_id IS DISTINCT FROM review_source
           OR NEW.target_source_path IS DISTINCT FROM review_path
           OR NEW.expected_old_hash IS DISTINCT FROM review_old_hash
           OR NEW.expected_new_hash IS DISTINCT FROM review_new_hash
           OR NEW.expected_absent IS DISTINCT FROM (review_kind = 'create') THEN
            RAISE EXCEPTION 'Apply journal does not match immutable review';
        END IF;
        IF TG_OP = 'UPDATE' THEN
            IF (NEW.id, NEW.proposal_id, NEW.review_id, NEW.target_source_id,
                NEW.target_source_path, NEW.expected_old_hash, NEW.expected_absent,
                NEW.expected_new_hash, NEW.actor_client_type,
                NEW.actor_external_chat_id, NEW.actor_external_user_id,
                NEW.intent_created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.proposal_id, OLD.review_id, OLD.target_source_id,
                OLD.target_source_path, OLD.expected_old_hash, OLD.expected_absent,
                OLD.expected_new_hash, OLD.actor_client_type,
                OLD.actor_external_chat_id, OLD.actor_external_user_id,
                OLD.intent_created_at) THEN
                RAISE EXCEPTION 'Apply binding is immutable';
            END IF;
            IF OLD.apply_status IN ('conflict', 'failed')
               AND NEW.apply_status = 'pending' THEN
                RAISE EXCEPTION 'Apply retry cannot erase its prior result';
            END IF;
            IF OLD.index_status = 'failed' AND NEW.index_status = 'pending' THEN
                RAISE EXCEPTION 'Index retry cannot erase its prior result';
            END IF;
            IF OLD.apply_status = 'applied' AND NEW.apply_status != 'applied' THEN
                RAISE EXCEPTION 'Successful apply is terminal';
            END IF;
            IF OLD.apply_status = 'applied' AND
               (NEW.actual_hash, NEW.apply_error_class, NEW.apply_attempts,
                NEW.last_apply_attempt_at, NEW.applied_at)
               IS DISTINCT FROM
               (OLD.actual_hash, OLD.apply_error_class, OLD.apply_attempts,
                OLD.last_apply_attempt_at, OLD.applied_at) THEN
                RAISE EXCEPTION 'Successful apply result is immutable';
            END IF;
            IF OLD.index_status = 'indexed' AND NEW.index_status != 'indexed' THEN
                RAISE EXCEPTION 'Successful indexing is terminal';
            END IF;
            IF OLD.index_status = 'indexed' AND
               (NEW.index_error_class, NEW.index_attempts,
                NEW.last_index_attempt_at, NEW.indexed_at, NEW.updated_at)
               IS DISTINCT FROM
               (OLD.index_error_class, OLD.index_attempts,
                OLD.last_index_attempt_at, OLD.indexed_at, OLD.updated_at) THEN
                RAISE EXCEPTION 'Successful index result is immutable';
            END IF;
            IF OLD.refresh_proposal_id IS NOT NULL AND
               NEW.refresh_proposal_id IS DISTINCT FROM OLD.refresh_proposal_id THEN
                RAISE EXCEPTION 'Refresh proposal binding is immutable';
            END IF;
            IF OLD.index_status = 'indexed' THEN
                NEW.updated_at := OLD.updated_at;
            ELSE
                NEW.updated_at := now();
            END IF;
        END IF;
        IF NEW.refresh_proposal_id IS NOT NULL THEN
            EXECUTE format(
                'SELECT conversation_id FROM %I.proposals WHERE id = $1',
                TG_TABLE_SCHEMA
            ) INTO refresh_conversation USING NEW.refresh_proposal_id;
            IF refresh_conversation IS DISTINCT FROM proposal_conversation
               OR NEW.refresh_proposal_id = NEW.proposal_id THEN
                RAISE EXCEPTION 'Refresh proposal does not match apply conflict';
            END IF;
        END IF;
        RETURN NEW;
    END
    $$;

    CREATE OR REPLACE TRIGGER proposal_applies_guard
        BEFORE INSERT OR UPDATE ON proposal_applies
        FOR EACH ROW EXECUTE FUNCTION guard_proposal_apply();

    CREATE OR REPLACE FUNCTION forbid_apply_delete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'Apply audit record is durable';
    END
    $$;
    CREATE OR REPLACE TRIGGER proposal_applies_no_delete
        BEFORE DELETE ON proposal_applies
        FOR EACH ROW EXECUTE FUNCTION forbid_apply_delete();
END
$migration$;
"""
