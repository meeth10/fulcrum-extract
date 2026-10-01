"""One-off cleanup of data/financials.db after the income-statement fixes.

1. Drops rows from older runs that were filed under the wrong statement (Amazon
   docs from the first run_pipeline attempts: segment tables stored as 'balance_sheet').
2. Collapses duplicate documents (same entity/doc_type/fiscal_year/file) to the
   OLDEST id, whose rows are the ones the fixed ingest just rewrote.
3. Optionally drops an entity whose extraction was never real data (--drop-entity).
Run:  python scripts/cleanup_db.py data/financials.db [--drop-entity "Coal India Ltd"]
"""
import argparse, sqlite3

ap = argparse.ArgumentParser(); ap.add_argument("db"); ap.add_argument("--drop-entity", action="append", default=[])
a = ap.parse_args(); c = sqlite3.connect(a.db)

# 1. rows that can't be what their statement label says: revenue/ebit/net_income metrics under balance_sheet
n1 = c.execute("DELETE FROM line_items WHERE statement='balance_sheet' AND metric IN ('revenue','ebit','ebitda','net_income')").rowcount
# 2. duplicate documents
dups = c.execute("""SELECT id FROM documents d WHERE id > (SELECT MIN(id) FROM documents
                    WHERE entity=d.entity AND doc_type=d.doc_type AND fiscal_year=d.fiscal_year AND filepath=d.filepath)""").fetchall()
ids = [r[0] for r in dups]
n2 = 0
for i in ids:
    n2 += c.execute("DELETE FROM line_items WHERE document_id=?", (i,)).rowcount
    c.execute("DELETE FROM documents WHERE id=?", (i,))
# 3. entities with no usable data
n3 = 0
for e in a.drop_entity:
    n3 += c.execute("DELETE FROM line_items WHERE entity=?", (e,)).rowcount
    c.execute("DELETE FROM documents WHERE entity=?", (e,))
c.commit()
print(f"mis-filed rows removed: {n1}; duplicate-document rows removed: {n2} (docs {ids}); dropped-entity rows: {n3}")
