"""Dump option premium paths out of ``positions_snapshot`` into the fixture
``exit_replay`` reads.

READ-ONLY. This issues a single SELECT and writes a JSON file; it never
mutates the database.

`positions_snapshot` stores the whole book every ~30s, and each row's
`open_positions` carries `market_value`, `qty`, `multiplier` and
`avg_entry_price` per position. `market_value / (qty * multiplier)` is the
per-contract premium, so the snapshot table is a price recorder nobody
built on purpose.

Consecutive duplicate prices are collapsed (the reconciler ticks every 30s
but the mark only moves on a real print), keeping the first and last of
each run so timing survives. 21,218 rows -> 8,080 distinct price points.

    PGURL=postgresql://... python -m tests.eval.dump_option_paths \
        tests/eval/fixtures/option_paths.json

Committed output means `exit_replay` runs offline with no keys, like the
rest of `tests/eval`.
"""
import json, os, sys, subprocess

PGURL = os.environ["PGURL"]
SQL = r"""
select p->>'symbol'                              as occ,
       extract(epoch from s.captured_at)::bigint as ts,
       (p->>'qty')::numeric                      as qty,
       (p->>'multiplier')::numeric               as mult,
       (p->>'market_value')::numeric             as mv,
       (p->>'avg_entry_price')::numeric          as entry
from positions_snapshot s, jsonb_array_elements(s.open_positions) p
where (p->>'is_option')::bool
order by occ, s.captured_at
"""
out = subprocess.run(["psql", PGURL, "-t", "-A", "-F", "|", "-c", SQL],
                     capture_output=True, text=True, check=True).stdout

paths: dict[str, dict] = {}
for line in out.splitlines():
    if not line.strip():
        continue
    occ, ts, qty, mult, mv, entry = line.split("|")
    qty_f, mult_f, mv_f = float(qty), float(mult), float(mv)
    if qty_f == 0 or mult_f == 0:
        continue
    price = mv_f / (qty_f * mult_f)
    rec = paths.setdefault(occ, {"occ": occ, "entry": float(entry),
                                 "qty": int(qty_f), "multiplier": int(mult_f),
                                 "samples": []})
    rec["samples"].append([int(ts), round(price, 4)])

# collapse consecutive duplicate prices — the reconciler ticks every 30s but
# the mark only moves on a real print. Keeps the first and last of each run so
# timing is preserved while the file stays small.
for rec in paths.values():
    s = rec["samples"]
    kept = [s[0]]
    for i in range(1, len(s)):
        if s[i][1] != s[i - 1][1]:
            if kept[-1] is not s[i - 1]:
                kept.append(s[i - 1])
            kept.append(s[i])
    if kept[-1] is not s[-1]:
        kept.append(s[-1])
    rec["samples"] = kept

json.dump({"paths": sorted(paths.values(), key=lambda r: r["occ"])},
          open(sys.argv[1], "w"), indent=1)
tot = sum(len(r["samples"]) for r in paths.values())
print(f"{len(paths)} contracts, {tot} price samples -> {sys.argv[1]}")
