import json
from collections import defaultdict

# Path to your log file
LOG_FILE_PATH = "../mydata/3/unix_time.jsonl"

# Nested dictionary to isolate Uplink ('U') and Downlink ('D')
# Structure: direction -> localID_key -> set of sources
id_name_source_map = {
    "U": defaultdict(set),
    "D": defaultdict(set)
}

# 1. Read and parse the log file separating by direction
with open(LOG_FILE_PATH, "r") as file:
    for line_num, line in enumerate(file, 1):
        line = line.strip()
        if not line:
            continue
        try:
            log_entry = json.loads(line)
            direction = log_entry.get("dir")
            src = log_entry.get("src")
            local_ids = log_entry.get("localIDs", {})
            
            # Keep Uplink and Downlink points completely isolated
            if direction in id_name_source_map and src and local_ids:
                for key in local_ids.keys():
                    id_name_source_map[direction][key].add(src)
        except json.JSONDecodeError:
            print(f"Warning: Skipping invalid JSON on line {line_num}")

# Configuration for separate pretty printing
direction_labels = {
    "U": "UPLINK (U)",
    "D": "DOWNLINK (D)"
}

# 2. Analyze and print results per direction
for dir_key, dir_label in direction_labels.items():
    print("=" * 60)
    print(f"CROSS-CHECK RESULTS: Shared localID Names Across {dir_label} Sources")
    print("=" * 60)
    
    # Group shared ID names by the specific sources within this direction
    shared_names_by_sources = defaultdict(list)
    
    for key_name, sources in id_name_source_map[dir_key].items():
        if len(sources) > 1:  # Only look at keys appearing in more than one src
            sorted_sources = tuple(sorted(sources))
            shared_names_by_sources[sorted_sources].append(key_name)

    if not shared_names_by_sources:
        print(f"No shared localID names were found across {dir_label} sources.")
    else:
        for sources, name_list in shared_names_by_sources.items():
            sources_str = " <---> ".join(sources)
            print(f"\n[Shared by]: {sources_str}")
            print("-" * 60)
            for name in sorted(name_list):
                print(f"  • {name}")
                
    print("\n" + "=" * 60 + "\n")