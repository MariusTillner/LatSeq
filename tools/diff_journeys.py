import json
import sys
from collections import Counter


def load_journeys(path):
    journeys = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                journeys.append(json.loads(line))
    return journeys


def analyze_diff(old_path, new_path):
    print(f"Loading {old_path}...")
    old_j = load_journeys(old_path)
    print(f"Loading {new_path}...")
    new_j = load_journeys(new_path)

    print("\n" + "=" * 60)
    print(" TOTAL COUNT DIFFERENCE")
    print("=" * 60)
    print(f"  Old Journeys Count: {len(old_j):,}")
    print(f"  New Journeys Count: {len(new_j):,}")
    print(f"  Difference        : {len(new_j) - len(old_j):,} ({((len(new_j)-len(old_j))/len(old_j))*100:.1f}%)")

    # 1. Break down by Direction
    old_dirs = Counter(j.get('dir') for j in old_j)
    new_dirs = Counter(j.get('dir') for j in new_j)

    print("\n" + "-" * 60)
    print(" 1. BREAKDOWN BY DIRECTION (dir)")
    print("-" * 60)
    all_dirs = set(old_dirs.keys()) | set(new_dirs.keys())
    for d in sorted(all_dirs):
        o_cnt = old_dirs[d]
        n_cnt = new_dirs[d]
        diff = n_cnt - o_cnt
        print(f"  Direction '{d}': Old={o_cnt:,} | New={n_cnt:,} | Diff={diff:+,}")

    # 2. Break down by Final Destination (Last Event in Journey)
    old_dests = Counter(j['events'][-1]['dest'] for j in old_j if j.get('events'))
    new_dests = Counter(j['events'][-1]['dest'] for j in new_j if j.get('events'))

    print("\n" + "-" * 60)
    print(" 2. BREAKDOWN BY TERMINAL NODE (Last Event Dest)")
    print("-" * 60)
    all_dests = set(old_dests.keys()) | set(new_dests.keys())
    for dest in sorted(all_dests):
        o_cnt = old_dests[dest]
        n_cnt = new_dests[dest]
        diff = n_cnt - o_cnt
        if diff != 0:
            print(f"  Node '{dest}': Old={o_cnt:,} | New={n_cnt:,} | Diff={diff:+,}")

    # 3. Check Startpoint Line Numbers (Which initial events lost their journeys?)
    old_starts = {j['events'][0]['line_num']: j for j in old_j if j.get('events')}
    new_starts = {j['events'][0]['line_num']: j for j in new_j if j.get('events')}

    missing_start_lines = set(old_starts.keys()) - set(new_starts.keys())
    print("\n" + "-" * 60)
    print(" 3. STARTPOINT DROP ANALYSIS")
    print("-" * 60)
    print(f"  Startpoints that completed in OLD but failed/stuck in NEW: {len(missing_start_lines):,}")

    if missing_start_lines:
        sample_line = next(iter(missing_start_lines))
        sample_j = old_starts[sample_line]
        print(f"\n  [Sample Disappeared Journey Startpoint]")
        print(f"    Start Line : {sample_line}")
        print(f"    Direction  : {sample_j.get('dir')}")
        print(f"    Path Hops  : {' -> '.join(e['src'] for e in sample_j['events'])} -> {sample_j['events'][-1]['dest']}")
        print(f"    LocalIDs   : {sample_j.get('localIDs')}")

    # 4. Hop-by-Hop Transition Breakdown (Pinpoints over-branching nodes)
    old_hops = Counter()
    new_hops = Counter()

    for j in old_j:
        for e in j.get('events', []):
            if e.get('src') and e.get('dest'):
                old_hops[(e['src'], e['dest'])] += 1

    for j in new_j:
        for e in j.get('events', []):
            if e.get('src') and e.get('dest'):
                new_hops[(e['src'], e['dest'])] += 1

    print("\n" + "-" * 60)
    print(" 4. HOP-BY-HOP TRANSITION ANALYSIS (Node Changes)")
    print("-" * 60)
    all_hops = sorted(set(old_hops.keys()) | set(new_hops.keys()))
    
    print(f"  {'HOP TRANSITION':<35} | {'OLD COUNT':<10} | {'NEW COUNT':<10} | {'DIFF':<10}")
    print("  " + "-" * 73)
    
    changed_hops = 0
    for hop in all_hops:
        o, n = old_hops[hop], new_hops[hop]
        diff = n - o
        if diff != 0:
            changed_hops += 1
            hop_str = f"{hop[0]} -> {hop[1]}"
            print(f"  {hop_str:<35} | {o:<10,} | {n:<10,} | {diff:<+10,}")
            
    if changed_hops == 0:
        print("  All hop counts are identical across datasets.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 diff_journeys.py <old_journeys.jsonl> <new_journeys.jsonl>")
        sys.exit(1)
    analyze_diff(sys.argv[1], sys.argv[2])