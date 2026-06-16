#!/usr/bin/env python3

import sys
import json

def usage():
    sys.stdout.write("[rdtsctots] Usage: ./rdtsctots.py raw_file.lseq > file.jsonl\n")
    sys.exit(1)

class RdtscToTs:
    def __init__(self, filename):
        self.filename = filename
        self.lines = []
        self._load_cleanup_and_sort()

    def _load_cleanup_and_sort(self):
        """Streams lines to prevent RAM explosion, strips comments, and sorts data."""
        raw_data_lines = []
        
        with open(self.filename, 'r', encoding='utf-8') as f:
            for line in f:
                line_str = line.strip()
                # Skip empty lines or sheer comments/headers
                if not line_str or line_str.startswith("#"):
                    continue
                
                # Check for sync frames or data frames
                parts = line_str.split()
                if len(parts) >= 4:
                    raw_data_lines.append(line_str)
                    
        if not raw_data_lines:
            raise IOError(f"No valid data records found in {self.filename}")
            
        # Sort chronologically by the raw RDTSC tick count (the first column)
        self.lines = sorted(raw_data_lines, key=lambda x: int(x.split()[0]))

    def _get_offset_cpufreq(self):
        """Calculates CPU frequency by mapping RDTSC cycles to wall-clock sync flags."""
        # Find the first and last sync ('S') lines
        first_S = next(l for l in self.lines if ' S ' in l).split()
        last_S = next(l for l in self.lines[::-1] if ' S ' in l).split()
        
        cycle_offset = int(first_S[0])
        time_offset = float(first_S[3])
        
        # FIXED: Corrected the legacy copy-paste bug (last_S[3] minus first_S[3])
        cycles_delta = int(last_S[0]) - cycle_offset
        time_delta = float(last_S[3]) - time_offset
        
        if time_delta == 0:
            raise ZeroDivisionError("Sync intervals match perfectly; cannot compute CPU clock frequency.")
            
        cpufreq = int(cycles_delta / time_delta)
        return cycle_offset, time_offset, cpufreq
    
    def stream_jsonl(self):
        """Converts raw clock ticks to Unix timestamps and builds structured JSON strings."""
        cycle_offset, time_offset, cpufreq = self._get_offset_cpufreq()
        
        for line in self.lines:
            parts = line.split()
            if len(parts) < 4:
                continue
                
            direction = parts[1]
            # Skip the internal system sync frames from entering final downstream telemetry
            if direction == 'S':
                continue
                
            # Calculate exact hardware event timestamp 
            rdtsc_ticks = int(parts[0])
            unix_ts = ((rdtsc_ticks - cycle_offset) / cpufreq) + time_offset
            
            # Extract src/dest channels
            src_dest = parts[2].split("--")
            src = src_dest[0] if len(src_dest) > 0 else "unknown"
            dest = src_dest[1] if len(src_dest) > 1 else "unknown"
            
            # Parse localized contextual identifiers (e.g., fm526.sl5.IQsize46080)
            local_ids = {}
            raw_metadata = parts[3].lstrip(':')
            if raw_metadata:
                # Split metadata by the period character
                meta_chunks = raw_metadata.split('.')
                for chunk in meta_chunks:
                    # Isolate alphabetical key indicators from numerical metric values
                    # example: 'fm526' -> key: 'fm', value: 526
                    alpha_chars = ''.join([c for c in chunk if c.isalpha()])
                    num_chars = ''.join([c for c in chunk if c.isdigit() or c == '-'])
                    
                    if alpha_chars and num_chars:
                        try:
                            local_ids[alpha_chars] = int(num_chars)
                        except ValueError:
                            local_ids[alpha_chars] = chunk
                    else:
                        local_ids[chunk] = True

            # Compile into unified structured tracing payload
            json_packet = {
                "dir": direction,
                "ts": round(unix_ts, 9),
                "src": src,
                "dest": dest,
                "localIDs": local_ids
            }
            
            yield json.dumps(json_packet)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        usage()

    try:
        converter = RdtscToTs(sys.argv[1])
        for json_line in converter.stream_jsonl():
            sys.stdout.write(json_line + "\n")
    except Exception as e:
        sys.stderr.write(f"Conversion terminated unexpectedly: {e}\n")
        sys.exit(1)