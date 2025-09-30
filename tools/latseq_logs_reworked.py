#!/usr/bin/python3

import logging
import argparse
import sys
import re
import decimal
from pathlib import Path
from tqdm import tqdm
from collections import defaultdict


logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(funcName)s] %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

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


class LatSeqLogParser:
    """Parses and reconstructs latency journeys from latseq log files."""
    
    def __init__(self, filepath):
        self.filepath = Path(filepath)
        # Load the raw data
        self.raw_lines = self._read_log_file(filepath)
        # parse self.log_events
        self.events = self._parse_all_events()

    # Renamed to reflect the action of loading raw data
    def _read_log_file(self, log_file_path) -> list:
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
        if not kv_string:
            return {}
        
        parsed_dict = {}
        items = kv_string.split('.')
        
        for item in items:
            key_end_index = 0
            
            # Find the index of the first digit
            for i, char in enumerate(item):
                if char.isdigit():
                    key_end_index = i
                    break

            if key_end_index > 0:
                # Initial split assumes the value starts at the first digit
                key = item[:key_end_index]
                value = item[key_end_index:]
                
                # Check for and include a preceding hyphen for negative numbers
                if key_end_index > 0 and item[key_end_index - 1] == '-':
                    key = item[:key_end_index - 1] # Remove hyphen from key
                    value = item[key_end_index - 1:] # Include hyphen in value
                    
                parsed_dict[key] = value
                
        return parsed_dict

    def _parse_event_line(self, log_line, line_num):
        """
        Parses a single latseq log line assuming the Uplink (UL) format:
        [TS] U [src--dest] [prop]:[globalIDs]:[localIDs]...

        Example: 1758902154.6302049 U mac.handover--mac.hdr size1000:rnti9199:RMbuf3547781203.fm523.sl12.hqpid5
        """
        timestamp_str, direction, src_dest_field, param_string = log_line.split(" ")

        src, dest = src_dest_field.split('--')

        # 2. Split the parameter string by the colon (':') to separate the categories
        # This relies on the convention that the categories are strictly ordered.
        param_parts = [p.strip() for p in param_string.split(':')]

        # 3. Positional Assignment based on the required output structure
        # prop: The first segment
        prop_segment = param_parts[0] if len(param_parts) >= 1 else ""

        # globalIDs: The second segment
        global_ids_str = param_parts[1] if len(param_parts) >= 2 else ""

        # localIDs: The third segment and all subsequent segments
        local_ids_str = param_parts[2] if len(param_parts) >= 3 else ""

        # 4. Final Dictionary Construction
        parsed_event = {}
        parsed_event['line_num'] = line_num
        parsed_event['ts'] = decimal.Decimal(timestamp_str)
        parsed_event['dir'] = direction
        parsed_event['src'] = src
        parsed_event['dest'] = dest
        parsed_event['prop'] = self._parse_kv_string(prop_segment)
        parsed_event['globalIDs'] = self._parse_kv_string(global_ids_str)
        parsed_event['localIDs'] = self._parse_kv_string(local_ids_str)

        return parsed_event

    def _parse_all_events(self) -> list[dict]:
        """Parse all raw log lines into structured event dictionaries with a progress bar."""
        total_lines = len(self.raw_lines)
        logger.info(f"Starting to parse {total_lines} lines")
        events = []

        for line_num, line in tqdm(
            enumerate(self.raw_lines, start=1),
            total=total_lines,
            desc="Parsing log",
            unit="line",
            disable=not VERBOSITY  # Optional: hide when verbosity is off
        ):
            event = self._parse_event_line(line, line_num)
            if event is not None:
                events.append(event)

        logger.info(f"Log file parsed: {len(events)} events")
        return events

    def get_startpoint_events(self) -> list[dict]:
        """Return a list of events that are considered startpoints for journeys."""
        startpoints = []
        for event in self.events:
            if event['src'] in KWS_IN_D or event['src'] in KWS_IN_U:
                startpoints.append(event)
        return startpoints

    def get_uplink_events_by_src(self) -> dict[str, list[dict]]:
        uplink_dict = defaultdict(list)
        for event in self.events:
            if event['dir'] == 'U':
                if event['src'] not in KWS_IN_U:
                    uplink_dict[event['src']].append(event)
        return uplink_dict

    def get_downlink_events_by_src(self) -> dict[str, list[dict]]:
        downlink_dict = defaultdict(list)
        for event in self.events:
            if event['dir'] == 'D':
                if event['src'] not in KWS_IN_D:
                    downlink_dict[event['src']].append(event)
        return downlink_dict


class LatSeqJourneyRebuilder:
    def __init__(self, latseq_log_parser: LatSeqLogParser):
        """
        Initialize journey rebuilder with parsed events.
        """
        logger.info(f"Initialize {self.__class__.__name__}")
        self.startpoints: list[dict] = latseq_log_parser.get_startpoint_events()
        self.uplink_events_by_src: dict[str, dict] = latseq_log_parser.get_uplink_events_by_src()
        self.downlink_events_by_src: dict[str, dict] = latseq_log_parser.get_downlink_events_by_src()
        logger.info(f"Initialized {self.__class__.__name__} "
            f"with {len(self.startpoints)} startpoints, "
            f"{sum(map(len, self.uplink_events_by_src.values()))} uplink events, "
            f"{sum(map(len, self.downlink_events_by_src.values()))} downlink events")
        self.journeys: list[dict] = []

    def _rebuild_journeys(self) -> None:
        """Rebuild all journeys starting from startpoints."""
        logger.info(f"Starting to rebuild journeys from {len(self.startpoints)} startpoints")
        for startpoint in self.startpoints in tqdm(
            self.startpoints,
            desc="Rebuilding journeys from startpoints",
            unit="startpoint",
            disable=not VERBOSITY,
        ):
            journey = self._rebuild_journey_from_startpoint(startpoint)
            if journey is not None:
                self.journeys.append(journey)

        logger.info(f"Journeys rebuilt: {len(self.journeys)} journeys")

    def _rebuild_from_startingpoint(self, startpoint):
        pass
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
    
    args = parser.parse_args()
    input_log_path = args.log_file 
    log_processor = LatSeqLogParser(input_log_path)
    journey_rebuilder = LatSeqJourneyRebuilder(log_processor)
    print("test")
    

if __name__ == "__main__":
    main()