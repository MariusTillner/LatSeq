#!/usr/bin/python3

import logging
import argparse
import sys
import re
import decimal


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    stream=sys.stderr
)

# Reducing search space
DURATION_TO_SEARCH_PKT = decimal.Decimal(0.8) # USED to avoid accidental mismatch of points which are too far apart in the time domain, 0.05 are 50ms

# CONSTANTS
S_TO_MS = 1000

# full name of all points where segmentation can't happen; the user has to know if segmentation can happen; this improves perfomance as unnecessary search is avoided
#KWS_NO_SEGMENTATION = [
#                        'mac.demuxed--rlc.dec',
#                        'rlc.reassembled--pdcp.dec', // why?!?!
#                        'pdcp.dec--sdap.sdu',
#                        'pdcp.outoforderdeliv--sdap.sdu',
#                        'pdcp.reorderdeliv--sdap.sdu',
#                        'sdap.sdu--pdcp.hdr',
#                        'rlc.seg--mac.handover',
#                        'phy.retxdecfail--phy.prachpucch',
#                        'phy.CBdec--phy.retxdecfail',
#                        'mac.handover--mac.hdr', // why?!?!
#                        'mac.hdr--mac.retx'
#                      ]

KWS_NO_SEGMENTATION = [
                        # Downlink
                        'sdap.sdu--pdcp.hdr',
                        'pdcp.hdr--rlc.buffer',
                        'rlc.buffer--rlc.seg',
                        'rlc.seg--mac.handover',
                        'mac.hdr--mac.retx',
                        'mac.hdr--mac.dci',
                        'phy.cbseg--phy.ldpc',
                        'phy.ldpc--phy.scrambled',
                        'phy.scrambled--phy.modulated',
                        'phy.modulated--phy.resourcemapped',
                        'phy.resourcemapped--phy.antennamapped',
                        'phy.antennamapped--phy.csi',
                        'phy.csi--phy.ofdmidft',
                        'phy.ofdmidft--phy.SOUTHout',
                        # Uplink
                        'phy.SOUTHstart--phy.SOUTHend',
                        'phy.SOUTHend--phy.dft',
                        'phy.dft--phy.prachpucch',
                        'phy.CHest--phy.rbscalvl',
                        'phy.rbscalvl--phy.symbolproc',
                        'phy.TBdec--phy.srs',
                        'phy.srs--phy.rachuci',
                        'phy.retxdecfail--phy.prachpucch',
                        'phy.CBdec--phy.retxdecfail',
                        'mac.demuxed--rlc.dec',
                        'rlc.dec--rlc.reassembled',
                        'pdcp.reorderdeliv--sdap.sdu',
                        'pdcp.outoforderdeliv--sdap.sdu',
                        'pdcp.dec--sdap.sdu'
]

KWS_IN_D = ['sdap.sdu']
KWS_OUT_D = ['phy.out']
KWS_IN_U = ['phy.SOUTHstart']
KWS_OUT_U = ['gtp.out', 'phy.retxdrop', 'pdcp.rcvdsmallerdeliv']
VERBOSITY = True  # Verbosity for rebuild phase False by default; only shows progress bar when MULTIPROCESSING is False
MULTIPROCESSING = False # can be set to true in file or with args (see args.multiprocessing)
TRIM = False # can be set to true in file or with args (see args.trimlog), trims the input log file to 2 sec before and after the first/last meaningful line in the log


class TraceProcessor:
    """Handles the loading and processing of latseq log files."""
    
    def __init__(self, log_file_path):
        self.log_file_path = log_file_path
        # Load the raw data
        self.log_events = self._load_raw_lines(log_file_path)
        # parse self.log_events
        self.parsed_data = self._parse_log_events()
        # list of rebuilded journeys
        self.journeys = list()

    # Renamed to reflect the action of loading raw data
    def _load_raw_lines(self, log_file_path) -> list:
        """Reads the log file and returns a list of raw lines."""
        raw_trace_lines = []
        try:
            # Use 'f' for the standard file handle
            with open(log_file_path, 'r') as f:
                raw_trace_lines = f.readlines()
        except FileNotFoundError:
            # ... error handling
            pass
            
        return raw_trace_lines

    def _parse_kv_string(self, kv_string):
        """
        Converts a string of key-value pairs (e.g., 'size1000.rnti9199.hqpid5') 
        into a dictionary {'size': '1000', 'rnti': '9199', 'hqpid': '5'}.
        """
        if not kv_string:
            return {}
        
        parsed_dict = {}
        
        # 2. Split the normalized string into individual items (e.g., "size1000", "rnti9199")
        items = [item.strip() for item in kv_string.split('.') if item.strip()]
        
        # 3. Iterate through items and split key/value
        for item in items:
            # Regex: Capture one or more letters ([a-zA-Z]+) as the KEY, 
            # and everything else (which must start with a number [0-9].*) as the VALUE
            match = re.match(r"([a-zA-Z]+)([0-9].*)", item)
            
            if match:
                key, value = match.groups()
                parsed_dict[key] = value
                
        return parsed_dict

    def _parse_log_line(self, log_line):
        """
        Parses a single latseq log line assuming the Uplink (UL) format:
        [TS] U [src--dest] [prop]:[globalIDs]:[localIDs]...

        Example: 1758902154.6302049 U mac.handover--mac.hdr size1000:rnti9199:RMbuf3547781203.fm523.sl12.hqpid5
        """

        # 1. Regex to capture the fixed fields and the remaining parameters
        # Group 1: Timestamp (TS)
        # Group 2: Direction (D/U)
        # Group 3: Source (src)
        # Group 4: Destination (dest)
        # Group 5: The entire parameter string (e.g., "size1000:rnti9199:RMbuf...")

        ts_str, dir_char, src_dest_str, param_string = log_line.split(" ")

        src, dest = src_dest_str.split('--')

        # 2. Split the parameter string by the colon (':') to separate the categories
        # This relies on the convention that the categories are strictly ordered.
        param_parts = [p.strip() for p in param_string.split(':')]

        # 3. Positional Assignment based on the required output structure
        # prop: The first segment
        prop_str = param_parts[0] if len(param_parts) >= 1 else ""

        # globalIDs: The second segment
        global_ids_str = param_parts[1] if len(param_parts) >= 2 else ""

        # localIDs: The third segment and all subsequent segments
        local_ids_str = param_parts[2] if len(param_parts) >= 3 else ""

        # 4. Final Dictionary Construction
        parsed_event = {}
        parsed_event['ts'] = float(ts_str)
        parsed_event['dir'] = dir_char
        parsed_event['src'] = src
        parsed_event['dest'] = dest
        parsed_event['prop'] = self._parse_kv_string(prop_str)
        parsed_event['globalIDs'] = self._parse_kv_string(global_ids_str)
        parsed_event['localIDs'] = self._parse_kv_string(local_ids_str)

        return parsed_event

    def _parse_log_events(self) -> None:
        parsed_data = list()
        for line in self.log_events:
            new_parsed_event_dict = self._parse_log_line(line)
            parsed_data.append(new_parsed_event_dict)

        return parsed_data

    def rebuild_journeys(self):
        pass
        #print(self.parsed_data)


# --- Main Execution Block ---

def main():
    """
    Parses command-line arguments and executes the journey reconstruction task.
    """
    
    # 1. Setup Argument Parser
    parser = argparse.ArgumentParser(
        description="Reconstructs individual packet traces and calculates latency from latseq log files."
    )
    
    # Argument for the log file path
    parser.add_argument(
        '-l', '--log-file', 
        required=True, 
        help="Path to the input latseq log file (e.g., unix_time.lseq)."
    )
    
    # Argument for the task that should be done
    parser.add_argument(
        '-j', '--journeys',
        action='store_true',
        required=False,
        help="Indicates that journeys should be rebuild and printed or written to output file"
    )

    # Argument for the output file path
    parser.add_argument(
        '-o', '--output-file',
        required=False,
        help="Path for the output JSON journey file (e.g., journeys_separated.lseqj)."
    )
    
    # NOTE: The '--journeys' flag is redundant if this is the script's only job.
    # It's better to assume the action and allow for future sub-commands if needed.
    # However, keeping it simple for now and removing the unnecessary check.

    # 2. Parse Arguments
    args = parser.parse_args()
    
    # 3. Execution Logic
    # Use the idiomatic variable name from the parsing step
    input_log_path = args.log_file 
    output_results_path = args.output_file # Use the clearer output name
    # Instantiate the processor class (using the preferred 'TraceProcessor')
    # NOTE: Replace 'TraceProcessor' with your actual class name if different
    log_processor = TraceProcessor(input_log_path)
    
    # Start the main processing task
    # Use the clearer variable name for the output file
    log_processor.rebuild_journeys()

if __name__ == "__main__":
    main()