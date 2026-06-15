#!/usr/bin/env python3

import json
import operator
import os
import resource
import statistics
import math
from collections import defaultdict

# Map string operators to Python functions
OPERATORS = {
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le
}


def set_memory_governor(max_gb=12.0):
    """Restricts the process from consuming more than a specified amount of RAM."""
    # Convert Gigabytes to Bytes
    max_bytes = int(max_gb * 1024 * 1024 * 1024)
    
    # RLIMIT_AS controls the maximum area of virtual memory for the process
    try:
        resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
        print(f"🔒 Memory governor active: Script will abort if RAM exceeds {max_gb} GB")
    except ValueError as e:
        print(f"⚠️ Could not set memory limit: {e}")


class JourneyStream:
    def __init__(self, file_path):
        self.file_path = file_path
        self.total_lines = self._quick_count_lines()
        self.stream = self._read_lines()

    def _quick_count_lines(self):
        """Quickly counts lines by reading raw binary chunks to find newlines."""
        print(f"Counting total lines in {self.file_path}...")
        lines = 0
        try:
            with open(self.file_path, "rb") as f:
                buf_size = 1024 * 1024 * 10
                read_chunk = f.read
                buf = read_chunk(buf_size)
                while buf:
                    lines += buf.count(b"\n")
                    buf = read_chunk(buf_size)
            print(f"Total lines found: {lines:,}")
            return lines
        except Exception as e:
            print(f"Could not pre-count lines ({e}). Progress percentage will be disabled.")
            return 0

    def _read_lines(self):
        print(f"Opening and streaming from: {self.file_path}")
        lines_read = 0
        with open(self.file_path, "r", encoding="utf-8") as infile:
            for line in infile:
                if line.strip():
                    lines_read += 1
                    if lines_read % 10000 == 0:
                        if self.total_lines > 0:
                            percentage = (lines_read / self.total_lines) * 100
                            print(f" -> Read {lines_read:,} lines ({percentage:.2f}%) from input...", end="\r")
                        else:
                            print(f" -> Read {lines_read:,} lines from input...", end="\r")
                        
                    yield json.loads(line)
        print(f"\nFinished reading input file. Total lines read: {lines_read:,}")

    def filter_by_localid(self, localid, comp_str, value):
        op_func = OPERATORS.get(comp_str)
        if not op_func:
            raise ValueError(f"Unsupported operator: {comp_str}")

        def generator_filter(current_stream):
            for journey in current_stream:
                local_ids = journey.get("localIDs", {})
                if localid in local_ids and op_func(local_ids[localid], value):
                    yield journey
        self.stream = generator_filter(self.stream)
        return self

    def filter_by_root(self, key, comp_str, value):
        op_func = OPERATORS.get(comp_str)
        if not op_func:
            raise ValueError(f"Unsupported operator: {comp_str}")

        def generator_filter(current_stream):
            for journey in current_stream:
                if key in journey and op_func(journey[key], value):
                    yield journey
        self.stream = generator_filter(self.stream)
        return self

    def filter_by_event_src(self, src_name):
        """Filters journeys, keeping only those containing an event with a specific source."""
        def generator_filter(current_stream):
            for journey in current_stream:
                if any(event.get("src") == src_name for event in journey.get("events", [])):
                    yield journey
        self.stream = generator_filter(self.stream)
        return self

    # --- NEW INTERMEDIATE FILTER METHOD ---
    def filter_worst_journey_per_packet(self):
        """
        Consumes the current stream stage, groups all journeys by packet_id,
        and modifies the stream to yield ONLY the single journey with the highest
        latency for each unique packet_id.
        """
        print("Grouping journeys by Packet ID to isolate worst latency segments...")
        
        # Dictionary structure: { packet_id: [list_of_journeys] }
        packet_aggregator = defaultdict(list)
        
        # Fully consume previous stream filters to group fragments
        for journey in self.stream:
            p_id = journey.get("packet_id")
            if p_id is not None:
                packet_aggregator[p_id].append(journey)
            else:
                # If a journey has no packet_id, don't drop it; stream it through safely
                packet_aggregator[f"orphan_{id(journey)}"].append(journey)

        # Build a generator that only passes the peak bottleneck journeys
        def worst_journey_generator():
            for p_id, journeys in packet_aggregator.items():
                # Extract the single journey that exhibits maximum latency
                worst_journey = max(journeys, key=lambda j: j.get("latency_ms", 0.0))
                yield worst_journey

        self.stream = worst_journey_generator()
        return self

    # --- UPDATED DIAGNOSTIC PRETTY PRINT (Works downstream of worst journey filter) ---
    def analyze_packets(self, max_packets_to_print=3):
        """
        Terminal method that prints a streamlined execution trace of the 
        journeys currently inside the stream pipeline.
        """
        print(f"Rendering execution timelines for first {max_packets_to_print} journeys...\n")
        
        printed_count = 0
        for journey in self.stream:
            if printed_count >= max_packets_to_print:
                break
                
            p_id = journey.get("packet_id", "N/A")
            max_latency = journey.get("latency_ms", 0.0)
            direction = journey.get("dir", "N/A")
            j_id = journey.get("journey_id", "N/A")
            
            print("=" * 95)
            print(f" 📦 PACKET DIAGNOSTIC REPORT  |  PACKET ID: {p_id:<8}  |  Direction: {direction}")
            print("=" * 95)
            print(f" ├─ True Packet Latency (Worst Fragment):  {max_latency:.4f} ms")
            print("-" * 95)
            print(f" 🔍 TIMELINE ANALYSIS FOR WORST FRAGMENT (Journey ID: {j_id}):")
            print("-" * 95)
            
            events = journey.get("events", [])
            if len(events) < 2:
                print("    Not enough internal state events available to construct sub-hops.")
                continue

            deltas = []
            for i in range(len(events) - 1):
                ev_curr = events[i]
                ev_next = events[i+1]
                delta_ms = (ev_next.get("ts", 0) - ev_curr.get("ts", 0)) * 1000.0
                deltas.append((ev_curr, delta_ms))

            for ev_curr, delta_ms in deltas:
                src = ev_curr.get("src", "unknown")
                layer = f"[{src.split('.')[0].upper():<4}]"
                pct = int((delta_ms / max_latency) * 100) if max_latency > 0 else 0
                pct = min(max(pct, 0), 100)
                bar = "█" * (pct // 5)
                
                print(f"  {layer}  {src:<25} [{delta_ms:+8.2f} ms]  {bar:<20} {pct:3}%")
                
            print("\n" + "_" * 95 + "\n")
            printed_count += 1

    def statistics(self, target_key, percentiles=None, is_localid=False):
        if percentiles is None:
            percentiles = [50, 90, 95, 99]
            
        print(f"Collecting data for statistical analysis on '{target_key}'...")
        values = []

        for journey in self.stream:
            if is_localid:
                val = journey.get("localIDs", {}).get(target_key)
            else:
                val = journey.get(target_key)
                
            if val is not None and isinstance(val, (int, float)):
                values.append(val)

        if not values:
            print("No matching numeric data found to compute statistics.")
            return {}

        values.sort()
        n = len(values)

        stats = {
            "count": n,
            "min": values[0],
            "max": values[-1],
            "mean": statistics.mean(values),
        }

        stats["percentiles"] = {}
        for p in percentiles:
            if p == 0:
                stats["percentiles"][f"p{p}"] = values[0]
            elif p == 100:
                stats["percentiles"][f"p{p}"] = values[-1]
            else:
                k = (n - 1) * (p / 100)
                f = math.floor(k)
                c = math.ceil(k)
                if f == c:
                    stats["percentiles"][f"p{p}"] = values[int(k)]
                else:
                    stats["percentiles"][f"p{p}"] = values[f] + (k - f) * (values[c] - values[f])

        print("\n" + "="*40)
        print(f" STATISTICAL REPORT FOR: {target_key}")
        print("="*40)
        print(f" Sample Count:  {stats['count']:,}")
        print(f" Minimum:       {stats['min']:.2f}")
        print(f" Maximum:       {stats['max']:.2f}")
        print(f" Mean (Avg):    {stats['mean']:.2f}")
        print("-" * 40)
        for pct, val in stats["percentiles"].items():
            print(f" Percentile {pct.upper()}: {val:.2f}")
        print("="*40 + "\n")

        return stats

    def save(self, output_path):
        print(f"Starting pipeline processing...")
        match_count = 0
        with open(output_path, "w", encoding="utf-8") as outfile:
            for journey in self.stream:
                outfile.write(json.dumps(journey) + "\n")
                match_count += 1
        print(f"Done!\n └─ Saved lines: {match_count:,}")


workspace_path = "../mydata/2"
unix_time_path = os.path.join(workspace_path, "unix_time.jsonl")
journeys_path = os.path.join(workspace_path, "journeys/journeys.jsonl")
output_path = os.path.join(workspace_path, "journeys/output.jsonl")

def main():

    set_memory_governor(max_gb=12.0)

    (
        JourneyStream(journeys_path)
        .filter_by_root("dir", "==", "U")
        .statistics("qammod", [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 99, 99.9, 99.99], is_localid=True)

    )
    #(
    #    JourneyStream(input_path)
    #    .statistics("nbRBs", [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 99, 99.9, 99.99], is_localid=True)
    #)

if __name__ == "__main__":
    main()