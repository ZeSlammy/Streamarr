from sqlmodel import SQLModel, create_engine, Session
from config import settings

engine = create_engine(f"sqlite:///{settings.db_path}", echo=False)


def init_db():
    SQLModel.metadata.create_all(engine)
    # SQLite doesn't add new columns when create_all runs on an existing table.
    # Apply additive migrations idempotently here.
    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(series)")}
        if "tmdb_id" not in cols:
            conn.exec_driver_sql("ALTER TABLE series ADD COLUMN tmdb_id INTEGER")
            conn.commit()

        # Defensive: if movie table existed from a partial Phase 2 attempt,
        # backfill any columns that were missing.
        movie_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(movie)")}
        if movie_cols:  # table exists, ensure expected columns
            additive = [
                ("tmdb_id", "INTEGER"),
                ("cover", "VARCHAR"),
                ("plot", "VARCHAR"),
                ("release_year", "VARCHAR"),
                ("genre", "VARCHAR"),
                ("rating", "VARCHAR"),
                ("rating_5based", "FLOAT"),
                ("category_id", "VARCHAR"),
                ("container_extension", "VARCHAR"),
                ("stream_url", "VARCHAR"),
                ("added_at", "DATETIME"),
                ("last_seen_in_catalog", "DATETIME"),
                ("last_synced", "DATETIME"),
                ("lang", "VARCHAR"),
                ("lang_source", "VARCHAR"),
                ("subs_only", "BOOLEAN DEFAULT 0"),
            ]
            for col_name, col_type in additive:
                if col_name not in movie_cols:
                    conn.exec_driver_sql(f"ALTER TABLE movie ADD COLUMN {col_name} {col_type}")
            conn.commit()

        # movie_tmdb defensive migrations (rename + adds)
        mt_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(movietmdb)")}
        if mt_cols:
            mt_additive = [
                ("title", "VARCHAR"),
                ("original_title", "VARCHAR"),
                ("overview", "VARCHAR"),
                ("release_date", "VARCHAR"),
                ("runtime", "INTEGER"),
                ("genres", "VARCHAR"),
                ("cast_field", "VARCHAR"),
                ("director", "VARCHAR"),
                ("poster_path", "VARCHAR"),
                ("backdrop_path", "VARCHAR"),
                ("collection_id", "INTEGER"),
                ("collection_name", "VARCHAR"),
                ("enriched_at", "DATETIME"),
                ("original_language", "VARCHAR"),
                ("not_found", "BOOLEAN DEFAULT 0"),
                ("last_attempt_at", "DATETIME"),
            ]
            for col_name, col_type in mt_additive:
                if col_name not in mt_cols:
                    conn.exec_driver_sql(f"ALTER TABLE movietmdb ADD COLUMN {col_name} {col_type}")
            conn.commit()


def get_session():
    with Session(engine) as session:
        yield session
