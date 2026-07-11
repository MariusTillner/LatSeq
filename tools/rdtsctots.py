#!/usr/bin/env python3

import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict

class RdtscToTs:
    def __init__(self, filename):
        self.filename = filename
        self.lines = []
        self._load_cleanup_and_sort()

    def _load_cleanup_and_sort(self):
        """Streams lines to prevent RAM explosion, strips comments, and sorts data."""
        sys.stderr.write(f"[+] Step 1: Loading and filtering rows from '{self.filename}'...\n")
        raw_data_lines = 0
        
        with open(self.filename, 'r', encoding='utf-8') as f:
            for line in f:
                line_str = line.strip()
                if not line_str or line_str.startswith("#"):
                    continue
                
                parts = line_str.split()
                if len(parts) >= 4:
                    self.lines.append(line_str)
                    raw_data_lines += 1
                    
        if not self.lines:
            raise IOError(f"No valid data records found in {self.filename}")
            
        sys.stderr.write(f"    -> Successfully loaded {raw_data_lines} data records.\n")
        sys.stderr.write("[+] Step 2: Sorting records chronologically by RDTSC ticks...\n")
        
        # Sort chronologically by the raw RDTSC tick count (the first column)
        self.lines.sort(key=lambda x: int(x.split()[0]))
        sys.stderr.write("    -> Sorting complete.\n")

        # Check for timestamp duplicates safely without overflowing the screen buffer
        self._check_timestamp_collisions()

    def _check_timestamp_collisions(self):
        """Scans sorted lines to identify and print identical RDTSC timestamps cleanly."""
        sys.stderr.write("[+] Step 2.5: Verifying timestamp uniqueness (checking for collisions)...\n")
        
        # Group entries by timestamp to find duplicates
        timestamp_map = defaultdict(list)
        for line in self.lines:
            parts = line.split()
            ticks = parts[0]
            src_dest = parts[2]
            timestamp_map[ticks].append(src_dest)
            
        collisions = {ticks: paths for ticks, paths in timestamp_map.items() if len(paths) > 1}
        
        if collisions:
            sys.stderr.write(f"    -> [WARNING] Found {len(collisions)} duplicate timestamp collisions!\n")
            for ticks, paths in collisions.items():
                total_occurrences = len(paths)
                
                # Truncate string output if a telemetry failure triggers a massive dump (e.g., RDTSC: 0)
                if total_occurrences > 6:
                    truncated_paths = ", ".join(paths[:6]) + f" ... [and {total_occurrences - 6} more]"
                else:
                    truncated_paths = ", ".join(paths)
                    
                sys.stderr.write(f"       * RDTSC: {ticks} occurred {total_occurrences} times across: {truncated_paths}\n")
        else:
            sys.stderr.write("    -> No timestamp collisions detected. Strict chronological order guaranteed.\n")

    def _get_offset_cpufreq(self):
        """Calculates CPU frequency by mapping RDTSC cycles to wall-clock sync flags."""
        sys.stderr.write("[+] Step 3: Calculating CPU clock synchronization factors...\n")
        
        # Isolate synchronization frames safely
        s_lines = [l for l in self.lines if ' S ' in l]
        
        if not s_lines:
            sys.stderr.write("    -> Warning: No 'S' sync lines found. Defaulting to 3.0 GHz baseline.\n")
            cycle_offset = int(self.lines[0].split()[0])
            time_offset = 0.0
            cpufreq = 3000000000
            return cycle_offset, time_offset, cpufreq
            
        first_S = s_lines[0].split()
        last_S = s_lines[-1].split()
        
        cycle_offset = int(first_S[0])
        time_offset = float(first_S[3])
        
        cycles_delta = int(last_S[0]) - cycle_offset
        time_delta = float(last_S[3]) - time_offset
        
        if time_delta == 0:
            raise ZeroDivisionError("Sync intervals match perfectly; cannot compute CPU clock frequency.")
            
        cpufreq = int(cycles_delta / time_delta)
        ghz = cpufreq / 1000000000
        sys.stderr.write(f"    -> Synchronized. Computed CPU Clock: {ghz:.4f} GHz\n")
        return cycle_offset, time_offset, cpufreq
    
    def parse_metadata_section(self, section_str):
        """Helper utility to parse dot-separated key=val elements within a block."""
        data = {}
        if not section_str:
            return data
            
        chunks = section_str.split('.')
        for chunk in chunks:
            if '=' in chunk:
                k, v = chunk.split('=', 1)
                try:
                    data[k.strip()] = int(v.strip())
                except ValueError:
                    data[k.strip()] = v.strip()
            elif chunk:
                data[chunk.strip()] = True
        return data

    def stream_jsonl(self):
        """Converts raw clock ticks to Unix timestamps and builds structured JSON strings."""
        cycle_offset, time_offset, cpufreq = self._get_offset_cpufreq()
        
        sys.stderr.write("[+] Step 4: Streaming and parsing log layout structures into JSONL...\n")
        
        processed_count = 0
        total_to_process = len(self.lines)
        
        for line in self.lines:
            parts = line.split()
            if len(parts) < 4:
                continue
                
            direction = parts[1]
            if direction == 'S':
                continue
                
            rdtsc_ticks = int(parts[0])
            unix_ts = ((rdtsc_ticks - cycle_offset) / cpufreq) + time_offset
            
            src_dest = parts[2].split("--")
            src = src_dest[0] if len(src_dest) > 0 else "unknown"
            dest = src_dest[1] if len(src_dest) > 1 else "unknown"
            
            # --- STRICT POSITION SPLIT: properties:globalIDs:localIDs ---
            meta_payload = parts[3]
            sections = meta_payload.split(':', 2) # Maximum 2 splits to yield up to 3 components
            
            # Pad array with empty strings if trailing positions are omitted in the log line
            while len(sections) < 3:
                sections.append('')
                
            properties = self.parse_metadata_section(sections[0])
            global_ids = self.parse_metadata_section(sections[1])
            local_ids = self.parse_metadata_section(sections[2])

            json_packet = {
                "dir": direction,
                "ts": round(unix_ts, 9),
                "src": src,
                "dest": dest,
                "properties": properties,
                "globalIDs": global_ids,
                "localIDs": local_ids
            }
            
            processed_count += 1
            if processed_count % 10000 == 0 or processed_count == total_to_process:
                pct = (processed_count / total_to_process) * 100
                sys.stderr.write(f"\r    -> Parsing progress: {processed_count}/{total_to_process} records completed ({pct:.2f}%)")
                sys.stderr.flush()
                
            yield json.dumps(json_packet)
            
        sys.stderr.write("\n[+] Success: Parsing processing thread safely shut down.\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert raw RDTSC trace sequences (.lseq) into highly-optimized JSONL records."
    )
    parser.add_argument(
        "-i", "--input", 
        type=str, 
        required=True,
        help="Path to the raw telemetry log sequence input file (e.g. data.lseq)"
    )
    parser.add_argument(
        "-o", "--output", 
        type=str, 
        default=None,
        help="Path to output file (e.g. data.jsonl). If omitted, streams directly to stdout."
    )

    args = parser.parse_args()

    if not Path(args.input).exists():
        sys.stderr.write(f"[!] Error: Input file '{args.input}' does not exist.\n")
        sys.exit(1)

    try:
        converter = RdtscToTs(args.input)
        
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as out_f:
                for json_line in converter.stream_jsonl():
                    out_f.write(json_line + "\n")
            sys.stderr.write(f"[+] Output successfully compiled into '{args.output}'\n")
        else:
            for json_line in converter.stream_jsonl():
                sys.stdout.write(json_line + "\n")
                
    except Exception as e:
        sys.stderr.write(f"\n[!] Conversion terminated unexpectedly: {e}\n")
        sys.exit(1)