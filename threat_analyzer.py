#!/usr/bin/env python3
"""
Module for analysis and threat detection based on server logs.
"""
import ipaddress
from collections import defaultdict
from datetime import datetime, timedelta
import logging
import json
import csv
import os
import io

from log_parser import parse_log_line, get_subnet, calculate_danger_score, is_ip_in_whitelist

# Logger for this module
logger = logging.getLogger('botstats.analyzer')

# Helper function for efficient reverse reading
def _read_lines_reverse(filename, buf_size=8192):
    """Read a file line by line backwards, memory efficiently."""
    with open(filename, 'rb') as f:
        segment = None
        offset = 0
        f.seek(0, os.SEEK_END)
        file_size = remaining_size = f.tell()
        while remaining_size > 0:
            offset = min(file_size, offset + buf_size)
            f.seek(file_size - offset)
            buffer = f.read(min(remaining_size, buf_size))
            # Ensure buffer is string
            try:
                buffer_str = buffer.decode('utf-8')
            except UnicodeDecodeError:
                # Handle potential decoding errors, e.g., skip or replace
                buffer_str = buffer.decode('utf-8', errors='ignore')
                
            remaining_size -= buf_size
            lines = buffer_str.splitlines(True) # Keep line endings
            if segment is not None:
                # If the previous chunk starts right after the previous one's end
                if buffer_str[-1] != '\n':
                     # Handle the case where a line is split across chunks
                     lines[-1] += segment
                else:
                    # The segment is a complete line by itself
                    yield segment
            segment = lines[0]
            # Yield lines in reverse order
            for i in range(len(lines) - 1, 0, -1):
                 if lines[i]:
                     yield lines[i]
        # Don't forget the last segment if it exists
        if segment is not None:
            yield segment

class ThreatAnalyzer:
    """
    Class for analyzing logs and detecting potential threats.
    """
    
    def __init__(self, rpm_threshold=100, whitelist=None):
        """
        Initializes the threat analyzer.
        
        Args:
            rpm_threshold (float): Requests per minute threshold to consider an IP suspicious
            whitelist (list): List of IPs or subnets that should never be blocked
        """
        self.rpm_threshold = rpm_threshold
        self.whitelist = whitelist or []
        self.ip_data = defaultdict(lambda: {'times': [], 'urls': [], 'useragents': []})
        self.subnet_data = defaultdict(list)
        self.unified_threats = []
        self.blocked_targets = set()
        
    def load_whitelist_from_file(self, whitelist_file):
        """
        Loads a whitelist from a file.
        
        Args:
            whitelist_file (str): Path to the file with the whitelist
            
        Returns:
            int: Number of entries loaded
        """
        if not os.path.exists(whitelist_file):
            logger.error(f"Whitelist file not found: {whitelist_file}")
            return 0
            
        try:
            with open(whitelist_file, 'r') as f:
                # Filter empty lines and comments
                self.whitelist = [
                    line.strip() for line in f 
                    if line.strip() and not line.strip().startswith('#')
                ]
            logger.info(f"Whitelist loaded with {len(self.whitelist)} entries from {whitelist_file}")
            return len(self.whitelist)
        except Exception as e:
            logger.error(f"Error loading whitelist from {whitelist_file}: {e}")
            return 0
    
    def analyze_log_file(self, log_file, start_date=None):
        """
        Analyzes a complete log file. Reads forwards if start_date is None,
        reads backwards from the end if start_date is provided, stopping
        when lines older than start_date are found.
        
        Args:
            log_file (str): Path to the log file
            start_date (datetime, optional): Date from which to analyze
            
        Returns:
            int: Number of entries processed
        """
        total_processed = 0
        try:
            if start_date:
                logger.info(f"Processing log file in reverse (newest first), stopping before {start_date}")
                # Process in reverse, stopping at start_date
                for line in _read_lines_reverse(log_file):
                    data = parse_log_line(line)
                    if data is None:
                        continue
                        
                    # Get date and time from the log
                    dt_str = data['datetime'].split()[0]
                    try:
                        dt = datetime.strptime(dt_str, '%d/%b/%Y:%H:%M:%S')
                    except ValueError:
                        continue # Skip malformed date
                        
                    # Stop if we reached lines older than start_date
                    if dt < start_date:
                        logger.info(f"Reached entry older than {start_date}. Stopping reverse scan.")
                        break 
                        
                    ip = data['ip']
                    if is_ip_in_whitelist(ip, self.whitelist):
                        continue
                        
                    self.ip_data[ip]['times'].append(dt)
                    self.ip_data[ip]['urls'].append(data['request'])
                    self.ip_data[ip]['useragents'].append(data['useragent'])
                    total_processed += 1
            else:
                logger.info("Processing log file forwards (oldest first)")
                # Process forwards, reading the whole file
                with open(log_file, 'r') as f:
                    for line in f:
                        data = parse_log_line(line)
                        if data is None:
                            continue
                            
                        # Get date and time from the log
                        dt_str = data['datetime'].split()[0]
                        try:
                            dt = datetime.strptime(dt_str, '%d/%b/%Y:%H:%M:%S')
                        except ValueError:
                            continue # Skip malformed date
                            
                        ip = data['ip']
                        if is_ip_in_whitelist(ip, self.whitelist):
                            continue
                            
                        self.ip_data[ip]['times'].append(dt)
                        self.ip_data[ip]['urls'].append(data['request'])
                        self.ip_data[ip]['useragents'].append(data['useragent'])
                        total_processed += 1
                        
            logger.info(f"Finished processing. Analyzed {total_processed} log entries within the specified time frame.")
            return total_processed
            
        except FileNotFoundError:
            logger.error(f"File not found {log_file}")
            raise
        except Exception as e:
            logger.error(f"Error processing log file {log_file}: {e}")
            raise
    
    def identify_threats(self):
        """
        Identifies threats based on accumulated data.
        
        Returns:
            list: List of detected threats
        """
        self.subnet_data = defaultdict(list)
        
        # First step: analyze each IP and group by subnet
        logger.info("Analyzing IPs and grouping by subnets...")
        for ip, info in self.ip_data.items():
            times = sorted(info['times'])
            total_requests = len(times)
            if total_requests == 0:
                continue

            # Calculate RPM
            rpm = 0
            time_span = 0
            if total_requests >= 2:
                time_span = (times[-1] - times[0]).total_seconds()
                if time_span > 0:
                    rpm = (total_requests / (time_span / 60))

            # Evaluate suspicion by RPM
            has_suspicious_ua = False
            suspicious_ua = ""
            is_suspicious_by_rpm = rpm > self.rpm_threshold
            is_suspicious = is_suspicious_by_rpm

            if is_suspicious:
                danger_score = calculate_danger_score(rpm, total_requests, has_suspicious_ua)
                # Try to get the subnet (IPv4 or IPv6)
                subnet = get_subnet(ip)
                if subnet:
                    ip_info = {
                        'ip': ip,
                        'rpm': rpm,
                        'total_requests': total_requests,
                        'time_span': time_span,
                        'has_suspicious_ua': has_suspicious_ua,
                        'suspicious_ua': suspicious_ua,
                        'danger_score': danger_score,
                        'is_suspicious_by_rpm': is_suspicious_by_rpm
                    }
                    self.subnet_data[subnet].append(ip_info)

        # Second step: unify threats by subnet
        self.unified_threats = []
        logger.info(f"Evaluating {len(self.subnet_data)} subnets with suspicious IPs...")

        for subnet, ip_infos in self.subnet_data.items():
            subnet_total_requests = sum(info['total_requests'] for info in ip_infos)
            subnet_total_danger = sum(info['danger_score'] for info in ip_infos)
            subnet_ip_count = len(ip_infos)
            
            if subnet_ip_count > 1:  # Subnet-type threat
                threat = {
                    'type': 'subnet',
                    'id': subnet,
                    'danger_score': subnet_total_danger,
                    'total_requests': subnet_total_requests,
                    'ip_count': subnet_ip_count,
                    'details': sorted(ip_infos, key=lambda x: x['danger_score'], reverse=True)
                }
                self.unified_threats.append(threat)
            else:  # Individual IP threat
                ip_info = ip_infos[0]
                ip_addr_obj = ipaddress.ip_address(ip_info['ip'])
                threat = {
                    'type': 'ip',
                    'id': ip_addr_obj,
                    'danger_score': ip_info['danger_score'],
                    'rpm': ip_info['rpm'],
                    'total_requests': ip_info['total_requests'],
                    'time_span': ip_info['time_span'],
                    'has_suspicious_ua': ip_info['has_suspicious_ua'],
                    'suspicious_ua': ip_info['suspicious_ua'],
                    'is_suspicious_by_rpm': ip_info['is_suspicious_by_rpm']
                }
                self.unified_threats.append(threat)

        # Sort by danger score
        self.unified_threats = sorted(
            self.unified_threats, 
            key=lambda x: x['danger_score'], 
            reverse=True
        )
        
        logger.info(f"Identified {len(self.unified_threats)} threats in total")
        return self.unified_threats

    def get_top_threats(self, top=10):
        """
        Gets the most dangerous threats.
        
        Args:
            top (int): Number of threats to return
            
        Returns:
            list: The top N most dangerous threats
        """
        if not self.unified_threats:
            self.identify_threats()
            
        return self.unified_threats[:top]
        
    def export_results(self, format_type, output_file):
        """
        Exports the results to a file in a specific format.
        
        Args:
            format_type (str): Export format ('json', 'csv')
            output_file (str): Path of the output file
            
        Returns:
            bool: True if export was successful, False otherwise
        """
        if not self.unified_threats:
            logger.warning("No threats to export")
            return False
            
        try:
            if format_type.lower() == 'json':
                # Convert ipaddress objects to strings for JSON serialization
                json_threats = []
                for threat in self.unified_threats:
                    json_threat = threat.copy()
                    json_threat['id'] = str(json_threat['id'])
                    if 'details' in json_threat:
                        json_threat['details'] = json_threat['details']
                    json_threats.append(json_threat)
                    
                with open(output_file, 'w') as f:
                    json.dump(json_threats, f, indent=2)
                    
            elif format_type.lower() == 'csv':
                with open(output_file, 'w', newline='') as f:
                    # Define fields by threat type
                    fieldnames = ['type', 'id', 'danger_score', 'total_requests']
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    
                    for threat in self.unified_threats:
                        # Create a simplified version for CSV
                        csv_threat = {
                            'type': threat['type'],
                            'id': str(threat['id']),
                            'danger_score': threat['danger_score'],
                            'total_requests': threat['total_requests']
                        }
                        writer.writerow(csv_threat)
            else:
                logger.error(f"Unsupported export format: {format_type}")
                return False
                
            logger.info(f"Results exported to {output_file} in {format_type} format")
            return True
            
        except Exception as e:
            logger.error(f"Error exporting results: {e}")
            return False