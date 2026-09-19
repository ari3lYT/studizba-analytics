CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS universities (
  id bigserial PRIMARY KEY,
  source_key text NOT NULL UNIQUE,
  name text NOT NULL,
  source_url text NOT NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS raw_pages (
  id bigserial PRIMARY KEY,
  source_url text NOT NULL,
  kind text NOT NULL,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  status_code int NOT NULL,
  content_sha256 text NOT NULL,
  content_encoding text NOT NULL DEFAULT 'gzip',
  content bytea NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(source_url, content_sha256)
);
CREATE INDEX IF NOT EXISTS raw_pages_url_time_idx ON raw_pages(source_url, fetched_at DESC);

CREATE TABLE IF NOT EXISTS departments (
  id bigserial PRIMARY KEY,
  university_id bigint NOT NULL REFERENCES universities(id),
  source_key text NOT NULL,
  slug text NOT NULL,
  name text NOT NULL,
  source_url text NOT NULL,
  raw_hash text,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  missing_since timestamptz,
  active boolean NOT NULL DEFAULT true,
  UNIQUE(university_id, source_key)
);
CREATE INDEX IF NOT EXISTS departments_name_trgm ON departments USING gin(name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS teachers (
  id bigserial PRIMARY KEY,
  university_id bigint NOT NULL REFERENCES universities(id),
  source_id bigint NOT NULL,
  name text NOT NULL,
  normalized_name text NOT NULL,
  source_url text NOT NULL,
  description text,
  title text,
  overall_rating double precision,
  explanation_rating double precision,
  attitude_rating double precision,
  grades_rating double precision,
  declared_reviews_count int,
  raw_hash text,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  missing_since timestamptz,
  active boolean NOT NULL DEFAULT true,
  UNIQUE(university_id, source_id)
);
CREATE INDEX IF NOT EXISTS teachers_name_trgm ON teachers USING gin(name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS teacher_departments (
  teacher_id bigint NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
  department_id bigint NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  active boolean NOT NULL DEFAULT true,
  PRIMARY KEY(teacher_id, department_id)
);

CREATE TABLE IF NOT EXISTS teacher_snapshots (
  id bigserial PRIMARY KEY,
  teacher_id bigint NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
  observed_at timestamptz NOT NULL DEFAULT now(),
  overall_rating double precision,
  explanation_rating double precision,
  attitude_rating double precision,
  grades_rating double precision,
  declared_reviews_count int,
  description text,
  raw_page_id bigint REFERENCES raw_pages(id),
  snapshot_hash text NOT NULL,
  UNIQUE(teacher_id, snapshot_hash)
);
CREATE INDEX IF NOT EXISTS teacher_snapshots_history_idx ON teacher_snapshots(teacher_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS reviews (
  id bigserial PRIMARY KEY,
  teacher_id bigint NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
  source_id bigint NOT NULL,
  parent_source_id bigint,
  kind text,
  author_name text,
  author_url text,
  body text NOT NULL,
  body_hash text NOT NULL,
  vote_rating int,
  published_at timestamptz,
  date_raw text,
  source_url text NOT NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  changed_at timestamptz,
  active boolean NOT NULL DEFAULT true,
  search_vector tsvector GENERATED ALWAYS AS (to_tsvector('russian', coalesce(body,''))) STORED,
  UNIQUE(teacher_id, source_id)
);
CREATE INDEX IF NOT EXISTS reviews_search_idx ON reviews USING gin(search_vector);
CREATE INDEX IF NOT EXISTS reviews_body_trgm ON reviews USING gin(body gin_trgm_ops);
CREATE INDEX IF NOT EXISTS reviews_time_idx ON reviews(published_at DESC);

CREATE TABLE IF NOT EXISTS review_versions (
  id bigserial PRIMARY KEY,
  review_id bigint NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  body text NOT NULL,
  body_hash text NOT NULL,
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz,
  UNIQUE(review_id, body_hash)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
  id bigserial PRIMARY KEY,
  mode text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  status text NOT NULL DEFAULT 'running',
  stats jsonb NOT NULL DEFAULT '{}'::jsonb,
  error text
);

CREATE TABLE IF NOT EXISTS crawl_events (
  id bigserial PRIMARY KEY,
  crawl_run_id bigint REFERENCES crawl_runs(id),
  entity_type text NOT NULL,
  entity_id bigint,
  event_type text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  details jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS crawl_events_time_idx ON crawl_events(occurred_at DESC);

CREATE TABLE IF NOT EXISTS crawl_targets (
  id bigserial PRIMARY KEY,
  entity_type text NOT NULL,
  entity_id bigint NOT NULL,
  source_url text NOT NULL,
  priority double precision NOT NULL DEFAULT 1,
  next_due_at timestamptz NOT NULL DEFAULT now(),
  last_checked_at timestamptz,
  consecutive_unchanged int NOT NULL DEFAULT 0,
  error_count int NOT NULL DEFAULT 0,
  last_error text,
  UNIQUE(entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS crawl_targets_due_idx ON crawl_targets(next_due_at, priority DESC);

CREATE TABLE IF NOT EXISTS analyzer_versions (
  id bigserial PRIMARY KEY,
  version text NOT NULL UNIQUE,
  prompt text NOT NULL,
  json_schema jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS review_analyses (
  id bigserial PRIMARY KEY,
  review_id bigint NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  analyzer_version text NOT NULL,
  model text NOT NULL,
  input_hash text NOT NULL,
  source_body_hash text,
  analyzed_at timestamptz NOT NULL DEFAULT now(),
  result jsonb NOT NULL,
  overall_confidence double precision NOT NULL,
  usage jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(review_id, analyzer_version, model, input_hash)
);
CREATE INDEX IF NOT EXISTS review_analyses_result_idx ON review_analyses USING gin(result);
ALTER TABLE review_analyses ADD COLUMN IF NOT EXISTS source_body_hash text;
CREATE INDEX IF NOT EXISTS review_analyses_current_idx ON review_analyses(review_id, analyzer_version, source_body_hash);
UPDATE review_analyses a SET source_body_hash=r.body_hash
FROM reviews r WHERE a.review_id=r.id AND a.source_body_hash IS NULL;

CREATE TABLE IF NOT EXISTS analysis_queue (
  review_id bigint PRIMARY KEY REFERENCES reviews(id) ON DELETE CASCADE,
  priority double precision NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'pending',
  attempts int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Operational state only: credentials are never stored in PostgreSQL.
CREATE TABLE IF NOT EXISTS glm_account_stats (
  label text PRIMARY KEY,
  enabled boolean NOT NULL DEFAULT true,
  healthy boolean NOT NULL DEFAULT true,
  model text,
  proxy_label text,
  active_requests int NOT NULL DEFAULT 0,
  processed_count bigint NOT NULL DEFAULT 0,
  failed_count bigint NOT NULL DEFAULT 0,
  http_429_count bigint NOT NULL DEFAULT 0,
  network_error_count bigint NOT NULL DEFAULT 0,
  latency_total_ms double precision NOT NULL DEFAULT 0,
  latency_samples bigint NOT NULL DEFAULT 0,
  cooldown_until timestamptz,
  last_success_at timestamptz,
  last_error text,
  last_error_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE glm_account_stats ADD COLUMN IF NOT EXISTS started_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE glm_account_stats ADD COLUMN IF NOT EXISTS proxy_label text;

CREATE TABLE IF NOT EXISTS teacher_metrics (
  teacher_id bigint NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
  metric text NOT NULL,
  metric_version text NOT NULL,
  calculated_at timestamptz NOT NULL DEFAULT now(),
  score double precision NOT NULL,
  recent_score double precision,
  all_time_score double precision,
  sample_size int NOT NULL,
  evidence_count int NOT NULL,
  confidence double precision NOT NULL,
  filters jsonb NOT NULL DEFAULT '{}'::jsonb,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY(teacher_id, metric, metric_version)
);

CREATE TABLE IF NOT EXISTS department_metrics (
  department_id bigint NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
  metric text NOT NULL,
  metric_version text NOT NULL,
  calculated_at timestamptz NOT NULL DEFAULT now(),
  score double precision NOT NULL,
  sample_size int NOT NULL,
  confidence double precision NOT NULL,
  filters jsonb NOT NULL DEFAULT '{}'::jsonb,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY(department_id, metric, metric_version)
);
