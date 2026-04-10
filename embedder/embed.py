import os
import time
import psycopg2
from openai import OpenAI

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "openbrain")
DB_USER = os.environ.get("DB_USER", "openbrain")
DB_PASS = os.environ.get("DB_PASS", "openbrain-db-2026")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
EMBEDDING_MODEL = "text-embedding-3-small"

client = OpenAI()

TABLES = [
    {
        "name": "knowledge",
        "text_columns": ["title", "content", "category", "project"],
    },
    {
        "name": "memories",
        "text_columns": ["name", "description", "content", "memory_type"],
    },
]


def get_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )


def get_embedding(text: str) -> list[float]:
    response = client.embeddings.create(input=text, model=EMBEDDING_MODEL)
    return response.data[0].embedding


def build_text(row: dict, columns: list[str]) -> str:
    parts = []
    for col in columns:
        val = row.get(col)
        if val:
            parts.append(f"{col}: {val}")
    return "\n".join(parts)


def process_table(conn, table_config: dict) -> int:
    table = table_config["name"]
    text_cols = table_config["text_columns"]
    cols_sql = ", ".join(text_cols)

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, {cols_sql} FROM {table} WHERE embedding IS NULL LIMIT 50"
        )
        col_names = [desc[0] for desc in cur.description]
        rows = [dict(zip(col_names, r)) for r in cur.fetchall()]

    count = 0
    for row in rows:
        text = build_text(row, text_cols)
        if not text.strip():
            continue
        try:
            embedding = get_embedding(text)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {table} SET embedding = %s::vector, updated_at = NOW() WHERE id = %s",
                    (str(embedding), row["id"]),
                )
            conn.commit()
            count += 1
            print(f"  Embedded {table} id={row['id']}")
        except Exception as e:
            conn.rollback()
            print(f"  Error embedding {table} id={row['id']}: {e}")

    return count


def main():
    print(f"Embedding sidecar started (model={EMBEDDING_MODEL}, poll={POLL_INTERVAL}s)")

    # Wait for database to be ready
    for attempt in range(30):
        try:
            conn = get_connection()
            conn.close()
            print("Database connected.")
            break
        except Exception:
            print(f"Waiting for database... (attempt {attempt + 1})")
            time.sleep(2)
    else:
        print("Could not connect to database after 30 attempts. Exiting.")
        return

    while True:
        try:
            conn = get_connection()
            total = 0
            for table_config in TABLES:
                total += process_table(conn, table_config)
            conn.close()
            if total > 0:
                print(f"Embedded {total} records this cycle.")
        except Exception as e:
            print(f"Error in poll cycle: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
