#!/usr/bin/env python3
"""
Main script for log analysis and bot threat detection using Pandas and configurable strategies.
"""
import argparse
import re
from datetime import datetime, timedelta, timezone
import sys
import os
import logging
import ipaddress
import pandas as pd
import importlib # For dynamic strategy loading
from collections import defaultdict # For grouping /16s

# Import own modules
from log_parser import get_subnet, is_ip_in_whitelist # Keep imports minimal
# Import UFWManager and COMMENT_PREFIX directly if needed
import ufw_handler
from threat_analyzer import ThreatAnalyzer

# Logging configuration
LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

def setup_logging(log_file=None, log_level=logging.INFO):
    """
    Configure the logging system.
    
    Args:
        log_file (str, optional): Path to the log file
        log_level (int): Logging level
    """
    handlers = []
    
    # Always add console handler
    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    handlers.append(console)
    
    # Add file handler if specified
    if (log_file):
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(log_level)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        handlers.append(file_handler)
    
    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=handlers
    )

def calculate_start_date(time_window):
    """
    Calculate the start date according to the specified time window.
    
    Args:
        time_window (str): 'hour', 'day', or 'week'
    
    Returns:
        datetime: Datetime object corresponding to the start date
    """
    now = datetime.now()
    if time_window == 'hour':
        return now - timedelta(hours=1)
    elif time_window == 'day':
        return now - timedelta(days=1)
    elif time_window == 'week':
        return now - timedelta(weeks=1)
    return None

def main():
    parser = argparse.ArgumentParser(
        description='Analyzes logs using Pandas and configurable strategies, optionally blocks threats with UFW.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter # Show defaults in help
    )
    # --- File/Time Args ---
    parser.add_argument(
        '--file', '-f', required=False,
        help='Path of the log file to analyze (required unless --clean-rules is used).'
    )
    parser.add_argument(
        '--start-date', '-s', required=False, default=None,
        help='Analyze logs from this date. Format: dd/mmm/yyyy:HH:MM:SS.'
    )
    parser.add_argument(
        '--time-window', '-tw', required=False,
        choices=['hour', 'day', 'week'],
        help='Analyze logs from the last hour, day, or week (overrides --start-date).'
    )
    # --- Analysis Args ---
    parser.add_argument(
        '--top', '-n', type=int, default=10,
        help='Number of top threats (/24 or /64) to display/consider for blocking.'
    )
    parser.add_argument(
        '--whitelist', '-w',
        help='File with IPs/subnets to exclude from analysis.'
    )
    # --- Blocking Strategy Args ---
    parser.add_argument(
        '--block', action='store_true',
        help='Enable blocking of detected threats using UFW.'
    )
    parser.add_argument(
        '--block-strategy', default='volume_danger',
        choices=['volume_danger', 'volume_coordination', 'volume_peak_rpm', 'combined', 'peak_total_rpm', 'coordinated_sustained'], # Add new strategy
        help='Strategy used to score threats and decide on blocking.'
    )
    parser.add_argument(
        '--block-threshold', type=int, default=100,
        help='Base threshold: Minimum total requests for a subnet to be considered for blocking.'
    )
    parser.add_argument(
        '--block-danger-threshold', type=float, default=50.0,
        help='Strategy threshold: Minimum aggregated IP danger score (used by volume_danger, combined).'
    )
    parser.add_argument(
        '--block-ip-count-threshold', type=int, default=10,
        help='Strategy threshold: Minimum number of unique IPs (used by volume_coordination, combined).'
    )
    parser.add_argument(
        '--block-max-rpm-threshold', type=float, default=62.0,
        help='Strategy threshold: Minimum peak RPM from any IP (used by volume_peak_rpm).'
    )
    parser.add_argument(
        '--block-total-max-rpm-threshold', type=float, default=62.0,
        help='Strategy threshold: Minimum peak TOTAL SUBNET RPM (max requests per minute for the entire subnet) (used by peak_total_rpm).'
    )
    # --- Add new arguments for the coordinated_sustained strategy ---
    parser.add_argument(
        '--block-subnet-avg-rpm-threshold', type=float, default=60.0,
        help='Strategy threshold: Minimum average TOTAL SUBNET RPM (used by coordinated_sustained).'
    )
    parser.add_argument(
        '--block-min-timespan-seconds', type=int, default=1800, # Default 30 minutes
        help='Strategy threshold: Minimum duration (seconds) of subnet activity (first to last request) (used by coordinated_sustained).'
    )
    # --- End of new arguments ---
    parser.add_argument(
        '--block-duration', type=int, default=60,
        help='Duration of the UFW block in minutes (used for all blocks).'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Show UFW commands without executing them.'
    )
    # --- Output Args ---
    parser.add_argument(
        '--output', '-o',
        help='File to save the analysis results.'
    )
    parser.add_argument(
        '--format', choices=['json', 'csv', 'text'], default='text',
        help='Output format for the results file.'
    )
    parser.add_argument(
        '--log-file', help='File to save execution logs.'
    )
    parser.add_argument(
        '--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default='INFO',
        help='Log detail level.'
    )
    # --- Utility Args ---
    parser.add_argument(
        '--clean-rules', action='store_true',
        help='Run cleanup of expired UFW rules and exit.'
    )
    args = parser.parse_args()

    # --- Logging Setup ---
    log_level = getattr(logging, args.log_level)
    setup_logging(args.log_file, log_level)
    logger = logging.getLogger('botstats.main')

    # --- Clean Rules Mode ---
    if args.clean_rules:
        logger.info("Starting cleanup of expired UFW rules...")
        # Instance is already created here for cleanup
        ufw_manager_instance = ufw_handler.UFWManager(args.dry_run)
        count = ufw_manager_instance.clean_expired_rules()
        logger.info("Cleanup completed. Rules deleted: %d", count)
        return

    # --- File Validation ---
    if not args.file:
        parser.error("the following arguments are required: --file/-f (unless --clean-rules is used)")
        sys.exit(1)
    if not os.path.exists(args.file):
        logger.error(f"Error: File not found {args.file}")
        sys.exit(1)

    # --- Date Calculation ---
    start_date_utc = None
    # ... (logic to calculate start_date_utc from args.time_window or args.start_date - unchanged) ...
    now_local = datetime.now()
    if args.time_window:
        start_date_naive_local = calculate_start_date(args.time_window)
        if start_date_naive_local:
             start_date_aware_local = start_date_naive_local.astimezone()
             start_date_utc = start_date_aware_local.astimezone(timezone.utc)
             logger.info(f"Using time window: {args.time_window} (from {start_date_utc})")
    elif args.start_date:
        try:
            start_date_naive_local = datetime.strptime(args.start_date, '%d/%b/%Y:%H:%M:%S')
            start_date_aware_local = start_date_naive_local.astimezone()
            start_date_utc = start_date_aware_local.astimezone(timezone.utc)
            logger.info(f"Using start date: {start_date_utc}")
        except ValueError:
            logger.error("Error: Invalid date format. Use dd/mmm/yyyy:HH:MM:SS")
            sys.exit(1)


    # --- Load Strategy ---
    strategy_name = args.block_strategy
    try:
        strategy_module = importlib.import_module(f"strategies.{strategy_name}")
        # Assumes each strategy module has a class named 'Strategy'
        strategy_instance = strategy_module.Strategy()
        logger.info(f"Using blocking strategy: {strategy_name}")
        # Optional: Validate required config keys?
        # required_keys = strategy_instance.get_required_config_keys()
        # Check if args has all required_keys...
    except ImportError:
        logger.error(f"Could not load strategy module: strategies.{strategy_name}.py")
        sys.exit(1)
    except AttributeError:
        logger.error(f"Strategy module strategies.{strategy_name}.py does not contain a 'Strategy' class.")
        sys.exit(1)


    # --- Analysis ---
    # Pass rpm_threshold? Currently unused by analyzer directly.
    analyzer = ThreatAnalyzer(whitelist=None) # Whitelist loaded separately
    if args.whitelist:
        analyzer.load_whitelist_from_file(args.whitelist)

    logger.info(f"Starting analysis of {args.file}...")
    try:
        processed_count = analyzer.analyze_log_file(args.file, start_date_utc)
        if processed_count <= 0: # Check for < 0 (error) or == 0 (no data)
             logger.warning("No log entries processed or error during loading. Exiting.")
             sys.exit(0 if processed_count == 0 else 1)
    except Exception as e:
        logger.error(f"Error analyzing log file: {e}", exc_info=True)
        sys.exit(1)

    # Identify threats (/24 or /64)
    threats = analyzer.identify_threats()
    if not threats:
         logger.info("No threats identified based on initial aggregation.")
         sys.exit(0)

    # --- Apply Strategy, Score, and Sort (/24 or /64) ---
    logger.info(f"Applying '{strategy_name}' strategy to {len(threats)} potential threats...")
    # Keep track of threats marked for blocking
    blockable_threats = []
    for threat in threats:
        score, should_block, reason = strategy_instance.calculate_threat_score_and_block(threat, args)
        threat['strategy_score'] = score
        threat['should_block'] = should_block
        threat['block_reason'] = reason
        if should_block:
            blockable_threats.append(threat) # Add to list if marked for blocking

    # Sort all threats by the calculated strategy score (descending) for reporting
    threats.sort(key=lambda x: x.get('strategy_score', 0), reverse=True)
    logger.info("Threats scored and sorted.")

    # --- Blocking Logic ---
    blocked_targets_count = 0
    blocked_subnets_via_supernet = set() # Keep track of /24s blocked via /16

    if args.block:
        print("-" * 30)
        logger.info(f"Processing blocks (Dry Run: {args.dry_run})...")
        ufw_manager_instance = ufw_handler.UFWManager(args.dry_run)

        # 1. Identify and Block /16 Supernets (Simplified Logic)
        supernets_to_block = defaultdict(list)
        # Group blockable IPv4 /24 threats by their /16 supernet
        for threat in blockable_threats: # Iterate only through threats marked for blocking
            subnet = threat.get('id')
            if isinstance(subnet, ipaddress.IPv4Network) and subnet.prefixlen == 24:
                try:
                    supernet = subnet.supernet(new_prefix=16)
                    supernets_to_block[supernet].append(threat) # Store the actual threat dict
                except ValueError:
                    continue # Skip if supernet calculation fails

        # Process potential /16 blocks
        logger.info(f"Checking {len(supernets_to_block)} /16 supernets for potential blocking (>= 2 contained blockable /24s)...")
        for supernet, contained_blockable_threats in supernets_to_block.items():
            # Block /16 if it contains >= 2 blockable /24 subnets
            if len(contained_blockable_threats) >= 2:
                target_to_block_obj = supernet
                target_type = "Supernet /16"
                block_duration = args.block_duration # Use standard duration
                # Create a reason based on contained threats
                contained_ids_str = ", ".join([str(t['id']) for t in contained_blockable_threats])
                reason = f"contains >= 2 blockable /24 subnets ({contained_ids_str})"

                logger.info(f"Processing block for {target_type}: {target_to_block_obj}. Reason: {reason}")
                success = ufw_manager_instance.block_target(
                    subnet_or_ip_obj=target_to_block_obj,
                    block_duration_minutes=block_duration
                )
                if success:
                    blocked_targets_count += 1
                    action = "Blocked" if not args.dry_run else "Dry Run - Blocked"
                    print(f" -> {action} {target_type}: {target_to_block_obj} for {block_duration} minutes.")
                    # Add contained /24 subnets to the set to prevent double blocking
                    for contained_threat in contained_blockable_threats:
                        blocked_subnets_via_supernet.add(contained_threat['id'])
                else:
                    action = "Failed to block" if not args.dry_run else "Dry Run - Failed"
                    print(f" -> {action} {target_type}: {target_to_block_obj}.")
            # else: # No need for debug log if only 1 threat, it will be handled individually if in top N
            #      logger.debug(f"Supernet {supernet} only contained 1 blockable /24 subnet. Not blocking /16.")


        # 2. Process individual /24 or /64 Blocks (Top N from the *original* sorted list)
        logger.info(f"Processing top {args.top} individual threat blocks (/24 or /64)...")
        top_threats_to_consider = threats[:args.top] # Use the overall top N threats
        for threat in top_threats_to_consider:
            target_id_obj = threat['id'] # ipaddress.ip_network object

            # Skip if this subnet was already covered by a /16 block
            if target_id_obj in blocked_subnets_via_supernet:
                logger.info(f"Skipping block for {target_id_obj}: Already covered by blocked supernet {target_id_obj.supernet(new_prefix=16)}.")
                continue

            # Check if this specific threat (within the top N) was marked for blocking
            if threat.get('should_block'):
                target_to_block_obj = target_id_obj # Default to the subnet object
                target_type = "Subnet"
                block_duration = args.block_duration # Use standard duration

                # Check if it's a single IP subnet (existing logic)
                if threat.get('ip_count') == 1 and threat.get('details'):
                    try:
                        single_ip_str = threat['details'][0]['ip']
                        target_to_block_obj = ipaddress.ip_address(single_ip_str)
                        target_type = "Single IP"
                        logger.info(f"Threat {threat['id']} has only 1 IP. Targeting IP {target_to_block_obj} instead of the whole subnet.")
                    except (IndexError, KeyError, ValueError) as e:
                        logger.warning(f"Could not extract/convert single IP from details for subnet {threat['id']} despite ip_count=1: {e}. Blocking subnet instead.")
                        target_type = "Subnet"
                        target_to_block_obj = threat['id']

                logger.info(f"Processing block for {target_type}: {target_to_block_obj}. Reason: {threat.get('block_reason')}")
                success = ufw_manager_instance.block_target(
                    subnet_or_ip_obj=target_to_block_obj,
                    block_duration_minutes=block_duration
                )
                if success:
                    blocked_targets_count += 1
                    action = "Blocked" if not args.dry_run else "Dry Run - Blocked"
                    print(f" -> {action} {target_type}: {target_to_block_obj} for {block_duration} minutes.")
                else:
                    action = "Failed to block" if not args.dry_run else "Dry Run - Failed"
                    print(f" -> {action} {target_type}: {target_to_block_obj}.")
            # else: # No need to log if a top N threat wasn't blockable, it just wasn't
            #    logger.debug(f"Threat {threat['id']} in top {args.top} did not meet blocking criteria for strategy '{strategy_name}'.")

        print(f"Block processing complete. {blocked_targets_count} targets {'would be' if args.dry_run else 'were'} processed for blocking.")
        print("-" * 30)
    else:
        logger.info("Blocking is disabled (--block not specified).")


    # --- Reporting Logic ---
    # Report Top /24 or /64 Threats
    top_count = min(args.top, len(threats))
    print(f"\n=== TOP {top_count} INDIVIDUAL THREATS DETECTED (/24 or /64) (Sorted by Strategy Score: '{strategy_name}') ===")
    if args.block:
        action = "Blocked" if not args.dry_run else "[DRY RUN] Marked for blocking"
        print(f"--- {action} based on strategy '{strategy_name}' criteria applied to top {args.top} threats ---")
        print(f"--- NOTE: /16 supernets containing >= 2 blockable /24s may have been blocked instead ---")


    top_threats_report = threats[:top_count]

    for i, threat in enumerate(top_threats_report, 1):
        target_id_obj = threat['id']
        target_id_str = str(target_id_obj)
        strat_score_str = f"Score: {threat.get('strategy_score', 0):.2f}"
        req_str = f"{threat['total_requests']} reqs"
        ip_count_str = f"{threat['ip_count']} IPs"
        agg_danger_str = f"AggDanger: {threat.get('aggregated_ip_danger_score', 0):.2f}"
        subnet_total_avg_rpm_str = f"~{threat.get('subnet_total_avg_rpm', 0):.1f} avg_total_rpm"
        subnet_total_max_rpm_str = f"{threat.get('subnet_total_max_rpm', 0):.0f} max_total_rpm"

        metrics_summary = f"{req_str}, {ip_count_str}, {agg_danger_str}, {subnet_total_avg_rpm_str}, {subnet_total_max_rpm_str}"

        block_info = ""
        # Determine block status for reporting
        is_blockable = threat.get('should_block', False)
        is_top_n = i <= args.top
        covered_by_supernet = target_id_obj in blocked_subnets_via_supernet

        if args.block:
            if covered_by_supernet:
                # Indicate it was blocked via its /16 parent, regardless of top N or its own block status
                block_info = f" [COVERED BY /16 BLOCK]"
            elif is_blockable and is_top_n:
                # Show BLOCKED status only if it was blockable, in top N, and NOT covered by /16
                block_status = "[BLOCKED]" if not args.dry_run else "[DRY RUN - BLOCKED]"
                block_info = f" {block_status}"
            # No special indicator if it wasn't blockable or wasn't in top N (and not covered by /16)

        print(f"\n#{i} Subnet: {target_id_str} - {strat_score_str} ({metrics_summary}){block_info}")

        if threat['details']:
            print("  -> Top IPs (by Max RPM):")
            for ip_detail in threat['details']:
                 print(f"     - IP: {ip_detail['ip']} ({ip_detail['total_requests']} reqs, Score: {ip_detail['danger_score']:.2f}, AvgRPM: {ip_detail['avg_rpm']:.2f}, MaxRPM: {ip_detail['max_rpm']:.0f})")
        else:
             print("  -> No IP details available.")

    # --- Export Results ---
    if args.output:
        # Pass the main threats list to export
        if analyzer.export_results(args.format, args.output, config=args, threats=threats):
            logger.info(f"Results exported to {args.output} in {args.format} format")
        else:
            logger.error(f"Error exporting results to {args.output}")

    # --- Final Summary ---
    print(f"\nAnalysis completed using strategy '{strategy_name}'.")
    print(f"{blocked_targets_count} unique targets (subnets/IPs or /16 supernets) {'blocked' if not args.dry_run else 'marked for blocking'} in this execution.")
    print(f"From a total of {len(threats)} detected individual threats.")
    if args.block:
        print(f"Use '--clean-rules' periodically to remove expired rules.")

if __name__ == '__main__':
    main()

