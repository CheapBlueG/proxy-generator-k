from flask import Flask, render_template_string, request, jsonify
import re
import math
import requests
import os
import random
import string
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

# App version - INCREMENT THIS TO FORCE ALL BROWSERS TO REFRESH
APP_VERSION = '1.0.0-K'

# Environment Variables (set in Render):
# SHARED_IP2LOCATION_KEY - IP2Location.io API key (for detection)
# IPAPI_KEY - ip-api.com API key (for location/distance)
# SOAX_PACKAGE_ID - SOAX package ID (e.g., 334354)
# SOAX_PASSWORD - SOAX password
# PROXYEMPIRE_USER_ID - Proxy Empire user ID (e.g., e1008856ab)
# PROXYEMPIRE_PASSWORD - Proxy Empire password


# Cache control headers to prevent browser caching
@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

def get_env_config():
    """Get configuration from environment variables"""
    return {
        'ipapi_key': os.environ.get('IPAPI_KEY', ''),
        'soax_package_id': os.environ.get('SOAX_PACKAGE_ID', ''),
        'soax_password': os.environ.get('SOAX_PASSWORD', ''),
    }


def generate_session_id(length=16):
    """Generate random session ID"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


def geocode_address(address, mapbox_key):
    """Convert address to coordinates and location info using Mapbox"""
    try:
        response = requests.get(
            f"https://api.mapbox.com/geocoding/v5/mapbox.places/{requests.utils.quote(address)}.json",
            params={
                'access_token': mapbox_key,
                'limit': 1,
                'types': 'address,place,locality,neighborhood,postcode'
            },
            timeout=10
        )
        data = response.json()
        
        if not data.get('features'):
            return None
        
        feature = data['features'][0]
        coords = feature['center']
        
        # Extract location components
        context = {item['id'].split('.')[0]: item['text'] for item in feature.get('context', [])}
        
        return {
            'lat': coords[1],
            'lon': coords[0],
            'place_name': feature['place_name'],
            'city': context.get('place', context.get('locality', '')),
            'region': context.get('region', ''),
            'country': context.get('country', 'United States'),
            'country_code': 'us'  # Default to US, can be extracted from context
        }
    except Exception as e:
        return None


def build_soax_proxy(package_id, password, country='us', region=None, city=None, session_length=3600):
    """Build SOAX proxy string"""
    session_id = generate_session_id()
    
    # Build username with location parameters
    username_parts = [f"package-{package_id}"]
    
    if country:
        username_parts.append(f"country-{country.lower()}")
    if region:
        # Clean region name for SOAX format
        region_clean = region.lower().replace(' ', '+')
        username_parts.append(f"region-{region_clean}")
    if city:
        # Clean city name for SOAX format
        city_clean = city.lower().replace(' ', '+')
        username_parts.append(f"city-{city_clean}")
    
    username_parts.append(f"sessionid-{session_id}")
    username_parts.append(f"sessionlength-{session_length}")
    
    username = '-'.join(username_parts)
    
    return {
        'provider': 'SOAX',
        'full_string': f"{username}:{password}@proxy.soax.com:5000",
        'server': 'proxy.soax.com',
        'port': '5000',
        'username': username,
        'password': password,
        'session_id': session_id
    }


def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two points in miles"""
    R = 3959  # Earth's radius in miles
    
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    
    a = math.sin(delta_lat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    return R * c


def test_proxy(proxy_string, target_lat, target_lon, ipapi_key, max_distance=15):
    """Test a proxy - check distance ONLY with ip-api (no IP2Location detection)
    
    Flow:
    1. Connect through proxy to get exit IP
    2. Check location/distance with ip-api.com
    3. If distance > max_distance -> FAIL
    4. If mobile/flagged ISP -> FAIL
    5. Otherwise -> PASS
    """
    try:
        # Parse proxy string
        match = re.match(r'^(.+):(.+)@(.+):(\d+)$', proxy_string)
        if not match:
            return {'success': False, 'error': 'Invalid proxy format'}
        
        username = match.group(1)
        password = match.group(2)
        host = match.group(3)
        port = match.group(4)
        
        proxy_url = f"http://{username}:{password}@{host}:{port}"
        proxies = {"http": proxy_url, "https": proxy_url}
        
        # ===== STEP 1: Get proxy exit IP =====
        proxy_ip = None
        try:
            ip_response = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=5)
            proxy_ip = ip_response.json()['ip']
        except:
            try:
                ip_response = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=5)
                proxy_ip = ip_response.json()['origin'].split(',')[0].strip()
            except Exception as e:
                return {'success': False, 'error': f'Connection timeout'}
        
        if not proxy_ip:
            return {'success': False, 'error': 'Could not get proxy IP'}
        
        # ===== STEP 2: Check LOCATION with ip-api.com =====
        try:
            ipapi_response = requests.get(
                f"https://pro.ip-api.com/json/{proxy_ip}?key={ipapi_key}&fields=status,message,country,regionName,city,lat,lon,isp,org,as,mobile",
                timeout=5
            )
            ipapi_data = ipapi_response.json()
            
            if ipapi_data.get('status') == 'fail':
                return {'success': False, 'error': f"ip-api error: {ipapi_data.get('message')}"}
            
            lat = ipapi_data.get('lat', 0) or 0
            lon = ipapi_data.get('lon', 0) or 0
            city = ipapi_data.get('city', 'Unknown') or 'Unknown'
            region = ipapi_data.get('regionName', 'Unknown') or 'Unknown'
            country = ipapi_data.get('country', 'Unknown') or 'Unknown'
            isp = ipapi_data.get('isp', 'Unknown') or 'Unknown'
            org = ipapi_data.get('org', 'Unknown') or 'Unknown'
            as_name = ipapi_data.get('as', 'Unknown') or 'Unknown'
            ipapi_mobile = ipapi_data.get('mobile', False) == True
            
        except Exception as e:
            return {'success': False, 'error': f'ip-api failed: {str(e)}'}
        
        # ===== STEP 3: Check DISTANCE =====
        distance = haversine_distance(target_lat, target_lon, lat, lon)
        
        # Build result object
        result = {
            'success': True,
            'passed': False,
            'fail_reasons': [],
            'ip': proxy_ip,
            'city': city,
            'region': region,
            'country': country,
            'lat': lat,
            'lon': lon,
            'distance': distance,
            'isp': isp,
            'org': org,
            'as_name': as_name,
        }
        
        # Check distance
        if distance > max_distance:
            result['fail_reasons'].append(f'Distance {distance:.1f} miles > {max_distance} max')
            return result
        
        # Check mobile ISPs
        isp_lower = isp.lower()
        as_lower = as_name.lower()
        mobile_keywords = ['mobile', 'wireless', 'cellular', 'lte', '5g', '4g', '3g']
        mobile_carriers = ['at&t', 'att ', 'verizon', 't-mobile', 'tmobile', 'sprint', 'cricket', 'boost', 'metro pcs', 'metropcs', 'us cellular', 'uscellular']
        
        is_mobile = ipapi_mobile
        is_mobile = is_mobile or any(kw in isp_lower or kw in as_lower for kw in mobile_keywords)
        is_mobile = is_mobile or any(carrier in isp_lower or carrier in as_lower for carrier in mobile_carriers)
        
        if is_mobile:
            result['fail_reasons'].append(f'Mobile ISP: {isp}')
            return result
        
        # Check flagged ISPs
        flagged_isps = ['rcn', 'starlink']
        is_flagged = any(f in isp_lower or f in as_lower for f in flagged_isps)
        
        if is_flagged:
            result['fail_reasons'].append(f'Flagged ISP: {isp}')
            return result
        
        # ===== ALL CHECKS PASSED =====
        result['passed'] = True
        return result
        
    except Exception as e:
        return {'success': False, 'error': str(e)}


HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Proxy Generator K</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            color: #fff;
            padding: 20px;
        }
        
        .container {
            max-width: 700px;
            margin: 0 auto;
        }
        
        h1 {
            text-align: center;
            margin-bottom: 8px;
            font-size: 2rem;
            background: linear-gradient(90deg, #00d9ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .subtitle {
            text-align: center;
            color: #888;
            margin-bottom: 25px;
            font-size: 14px;
        }
        
        .card {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 16px;
            padding: 25px;
            margin-bottom: 20px;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #ddd;
        }
        
        .label-hint {
            font-weight: normal;
            color: #666;
            font-size: 12px;
        }
        
        .api-link {
            color: #00d9ff;
            text-decoration: none;
        }
        
        input[type="text"], select {
            width: 100%;
            padding: 14px;
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 8px;
            font-size: 14px;
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            transition: all 0.3s;
        }
        
        input[type="text"]:focus, select:focus {
            outline: none;
            border-color: #00d9ff;
            box-shadow: 0 0 0 3px rgba(0, 217, 255, 0.1);
        }
        
        .save-key {
            margin-top: 8px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            color: #888;
        }
        
        .btn {
            width: 100%;
            padding: 16px;
            background: linear-gradient(90deg, #00d9ff, #00ff88);
            border: none;
            border-radius: 8px;
            color: #1a1a2e;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 20px rgba(0, 217, 255, 0.3);
        }
        
        .btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }
        
        .config-status {
            background: rgba(0, 217, 255, 0.1);
            border: 1px solid rgba(0, 217, 255, 0.3);
            border-radius: 8px;
            padding: 12px 15px;
            margin-bottom: 20px;
            font-size: 13px;
        }
        
        .config-status.error {
            background: rgba(255, 71, 87, 0.1);
            border-color: rgba(255, 71, 87, 0.3);
            color: #ff4757;
        }
        
        .config-status.success {
            color: #00ff88;
        }
        
        .results {
            display: none;
        }
        
        .results.show {
            display: block;
        }
        
        .loading {
            text-align: center;
            padding: 40px;
        }
        
        .spinner {
            width: 50px;
            height: 50px;
            border: 4px solid rgba(0, 217, 255, 0.2);
            border-top-color: #00d9ff;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        .success-box {
            background: rgba(0, 255, 136, 0.1);
            border: 2px solid #00ff88;
            border-radius: 12px;
            padding: 25px;
            text-align: center;
        }
        
        .success-icon {
            font-size: 48px;
            margin-bottom: 15px;
        }
        
        .success-title {
            font-size: 20px;
            font-weight: 700;
            color: #00ff88;
            margin-bottom: 5px;
        }
        
        .success-subtitle {
            color: #888;
            font-size: 13px;
            margin-bottom: 20px;
        }
        
        .proxy-output {
            background: rgba(0, 0, 0, 0.4);
            border-radius: 8px;
            padding: 15px;
            margin: 15px 0;
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 12px;
            word-break: break-all;
            cursor: pointer;
            transition: all 0.2s;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .proxy-output:hover {
            border-color: #00d9ff;
            background: rgba(0, 217, 255, 0.1);
        }
        
        .proxy-output-label {
            font-size: 11px;
            color: #888;
            margin-bottom: 8px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }
        
        .proxy-output-value {
            color: #00ff88;
        }
        
        .parsed-output {
            text-align: left;
        }
        
        .parsed-row {
            margin: 8px 0;
            color: #aaa;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .parsed-row strong {
            color: #00d9ff;
            min-width: 80px;
        }
        
        .parsed-value {
            background: #1a1a2e;
            padding: 8px 12px;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s;
            flex: 1;
            word-break: break-all;
            font-family: 'Courier New', monospace;
            font-size: 13px;
            border: 1px solid #333;
        }
        
        .parsed-value:hover {
            background: #2a2a4e;
            border-color: #00d9ff;
        }
        
        .parsed-value:active {
            transform: scale(0.98);
        }
        
        .parsed-value.copied {
            background: #1a3a1a;
            border-color: #00ff88;
        }
        
        .copy-hint {
            font-size: 11px;
            color: #666;
            margin-top: 8px;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-top: 20px;
            text-align: left;
        }
        
        .stat-item {
            background: rgba(0, 0, 0, 0.2);
            padding: 10px;
            border-radius: 6px;
        }
        
        .stat-label {
            font-size: 11px;
            color: #666;
        }
        
        .stat-value {
            font-size: 14px;
            color: #fff;
            font-weight: 600;
        }
        
        .stat-value.good {
            color: #00ff88;
        }
        
        .stat-value.bad {
            color: #ff4757;
        }
        
        .location-info, .isp-info {
            background: rgba(0, 0, 0, 0.3);
            border-radius: 8px;
            padding: 15px;
            margin: 15px 0;
            text-align: left;
        }
        
        .location-info h4, .isp-info h4, .detection-info h4 {
            color: #00d9ff;
            margin-bottom: 10px;
            font-size: 14px;
        }
        
        .location-row {
            margin: 6px 0;
            font-size: 13px;
            color: #aaa;
        }
        
        .location-row strong {
            color: #fff;
        }
        
        .detection-info {
            background: rgba(0, 0, 0, 0.3);
            border-radius: 8px;
            padding: 15px;
            margin: 15px 0;
            text-align: left;
        }
        
        .detection-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
        }
        
        .detection-item {
            display: flex;
            justify-content: space-between;
            padding: 8px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 4px;
            font-size: 12px;
        }
        
        .detection-item.good {
            border-left: 3px solid #00ff88;
        }
        
        .detection-item.bad {
            border-left: 3px solid #ff4757;
            background: rgba(255, 71, 87, 0.1);
        }
        
        .detection-label {
            color: #888;
        }
        
        .detection-value {
            color: #fff;
            font-weight: 600;
        }
        
        .error-box {
            background: rgba(255, 71, 87, 0.1);
            border: 2px solid #ff4757;
            border-radius: 12px;
            padding: 25px;
            text-align: center;
        }
        
        .error-icon {
            font-size: 48px;
            margin-bottom: 15px;
        }
        
        .error-title {
            font-size: 18px;
            font-weight: 700;
            color: #ff4757;
            margin-bottom: 10px;
        }
        
        .error-message {
            color: #aaa;
            font-size: 14px;
        }
        
        .attempts-info {
            margin-top: 15px;
            padding: 10px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 6px;
            font-size: 12px;
            color: #888;
        }
        
        .fail-reasons {
            margin-top: 15px;
            padding: 15px;
            background: rgba(0, 0, 0, 0.3);
            border-radius: 6px;
            text-align: left;
            font-size: 12px;
            color: #ff4757;
        }
        
        .fail-reasons ul {
            margin: 10px 0 0 20px;
        }
        
        .fail-reasons li {
            margin: 5px 0;
        }
        
        .last-result {
            margin-top: 15px;
            padding: 15px;
            background: rgba(0, 0, 0, 0.3);
            border-radius: 6px;
            text-align: left;
            font-size: 12px;
            color: #aaa;
        }
        
        .distance-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            margin-top: 10px;
        }
        
        .distance-badge.excellent {
            background: rgba(255, 215, 0, 0.3);
            color: #ffd700;
            border: 1px solid #ffd700;
        }
        
        .distance-badge.good {
            background: rgba(0, 255, 136, 0.25);
            color: #00ff88;
        }
        
        .distance-badge.decent {
            background: rgba(144, 238, 144, 0.25);
            color: #90ee90;
        }
        
        .shared-badge {
            background: rgba(0, 217, 255, 0.2);
            color: #00d9ff;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 10px;
            margin-left: 8px;
            font-weight: 600;
        }
        
        .options-row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }
        
        @media (max-width: 600px) {
            .options-row {
                grid-template-columns: 1fr;
            }
            .stats-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎯 Proxy Generator <span style="background: #ff6b6b; color: white; padding: 3px 12px; border-radius: 12px; font-size: 16px; font-weight: bold; vertical-align: middle;">K</span></h1>
        <p class="subtitle">Distance-only checking • SOAX proxies</p>
        
        <div class="config-status" id="configStatus">
            Checking configuration...
        </div>
        
        <div class="card">
            <div class="form-group" id="mapboxFieldGroup">
                <label>
                    Mapbox API Key 
                    <span class="label-hint">— <a href="https://account.mapbox.com/access-tokens/" target="_blank" class="api-link">Get free key</a></span>
                </label>
                <input type="text" id="mapboxKey" placeholder="pk.eyJ1Ijo...">
                <div class="save-key">
                    <input type="checkbox" id="saveKey" checked>
                    <label for="saveKey" style="margin: 0; font-weight: normal;">Remember API key in browser</label>
                </div>
            </div>
            
            <div class="form-group">
                <label>Target Address</label>
                <input type="text" id="targetAddress" placeholder="123 Main St, Miami, FL 33101">
            </div>
            
            <div class="options-row">
                <div class="form-group">
                    <label>Max Distance (miles)</label>
                    <select id="maxDistance">
                        <option value="5" selected>5 miles</option>
                        <option value="10">10 miles</option>
                        <option value="15">15 miles</option>
                        <option value="25">25 miles</option>
                        <option value="50">50 miles</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Max Attempts</label>
                    <select id="maxAttempts">
                        <option value="5">5 attempts</option>
                        <option value="10" selected>10 attempts</option>
                        <option value="20">20 attempts</option>
                        <option value="30">30 attempts</option>
                    </select>
                </div>
            </div>
            
            <button class="btn" id="generateBtn" onclick="generateProxy()">
                🔍 Find Clean Proxy
            </button>
        </div>
        
        <div class="results" id="results">
        </div>
    </div>
    
    <script>
        // Client app version - MUST MATCH SERVER APP_VERSION
        const CLIENT_APP_VERSION = '1.0.0-K';
        let configReady = false;
        let currentConfigVersion = null;
        
        window.onload = async function() {
            // Load saved Mapbox key
            const savedMapboxKey = localStorage.getItem('mapbox_api_key');
            if (savedMapboxKey) {
                document.getElementById('mapboxKey').value = savedMapboxKey;
            }
            
            // Check configuration and version
            try {
                const response = await fetch('/config-status');
                const config = await response.json();
                
                const statusDiv = document.getElementById('configStatus');
                
                if (config.ready) {
                    statusDiv.className = 'config-status success';
                    statusDiv.innerHTML = '✅ <strong>System Ready</strong> — SOAX + ip-api (Distance-only mode)';
                    configReady = true;
                } else {
                    statusDiv.className = 'config-status error';
                    let missing = [];
                    if (!config.has_ipapi) missing.push('ip-api.com API key');
                    if (!config.has_soax) missing.push('SOAX credentials');
                    statusDiv.innerHTML = '❌ <strong>Configuration Missing</strong> — ' + missing.join(', ');
                }
            } catch (e) {
                document.getElementById('configStatus').className = 'config-status error';
                document.getElementById('configStatus').innerHTML = '❌ Failed to check configuration';
            }
        };
        
        function copyToClipboard(text, element) {
            navigator.clipboard.writeText(text).then(() => {
                // Add copied class for visual feedback
                element.classList.add('copied');
                
                // Store original content
                const original = element.innerHTML;
                const originalText = element.textContent;
                
                // Show copied feedback
                if (element.classList.contains('parsed-value')) {
                    // For individual values, show checkmark briefly
                    element.innerHTML = '✓ Copied!';
                    element.style.color = '#00ff88';
                    setTimeout(() => {
                        element.innerHTML = original;
                        element.style.color = '';
                        element.classList.remove('copied');
                    }, 1000);
                } else {
                    // For full proxy string box
                    element.innerHTML = '<span style="color: #00ff88;">✓ Copied!</span>';
                    setTimeout(() => {
                        element.innerHTML = original;
                        element.classList.remove('copied');
                    }, 1500);
                }
            });
        }
        
        async function generateProxy() {
            const mapboxKey = document.getElementById('mapboxKey').value.trim();
            const targetAddress = document.getElementById('targetAddress').value.trim();
            const maxDistance = document.getElementById('maxDistance').value;
            const maxAttempts = document.getElementById('maxAttempts').value;
            const resultsDiv = document.getElementById('results');
            const btn = document.getElementById('generateBtn');
            
            if (!mapboxKey) {
                alert('Please enter your Mapbox API key');
                return;
            }
            
            if (!targetAddress) {
                alert('Please enter a target address');
                return;
            }
            
            if (!configReady) {
                alert('System not configured. Contact admin to set up SOAX and IP2Location.');
                return;
            }
            
            // Save Mapbox key
            if (document.getElementById('saveKey').checked) {
                localStorage.setItem('mapbox_api_key', mapboxKey);
            } else {
                localStorage.removeItem('mapbox_api_key');
            }
            
            btn.disabled = true;
            btn.textContent = '🔍 Searching...';
            resultsDiv.className = 'results show';
            resultsDiv.innerHTML = `
                <div class="card">
                    <div class="loading">
                        <div class="spinner"></div>
                        <p>Finding a clean proxy near your target...</p>
                        <p style="font-size: 12px; color: #666; margin-top: 10px;">Testing proxies for detection & location...</p>
                    </div>
                </div>
            `;
            
            try {
                // Add timeout to fetch request (30 seconds max)
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 30000);
                
                const response = await fetch('/generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        target_address: targetAddress,
                        mapbox_key: mapboxKey,
                        max_distance: parseInt(maxDistance),
                        max_attempts: parseInt(maxAttempts)
                    }),
                    signal: controller.signal
                });
                
                clearTimeout(timeoutId);
                
                const data = await response.json();
                
                if (data.error) {
                    let failInfo = '';
                    if (data.last_fail_reasons && data.last_fail_reasons.length > 0) {
                        failInfo = '<div class="fail-reasons"><strong>Last proxy failed because:</strong><ul>' + 
                            data.last_fail_reasons.map(r => '<li>' + r + '</li>').join('') + 
                            '</ul></div>';
                    }
                    if (data.last_result) {
                        failInfo += '<div class="last-result"><strong>Last proxy tested:</strong><br>' +
                            'IP: ' + (data.last_result.ip || 'N/A') + '<br>' +
                            'Location: ' + (data.last_result.city || 'N/A') + ', ' + (data.last_result.region || 'N/A') + '<br>' +
                            'ISP: ' + (data.last_result.isp || 'N/A') + '<br>' +
                            'Distance: ' + (data.last_result.distance ? data.last_result.distance.toFixed(1) + ' miles' : 'N/A') + '<br>' +
                            'Proxy Detected (is_proxy): ' + (data.last_result.is_proxy === true ? '🚨 YES' : data.last_result.is_proxy === false ? '✅ NO' : '⚠️ ' + data.last_result.is_proxy) + '<br>' +
                            'Proxy Type: ' + (data.last_result.proxy_type || '-') + '<br>' +
                            'Fraud Score: ' + (data.last_result.fraud_score ?? 'N/A') +
                            '</div>';
                    }
                    resultsDiv.innerHTML = `
                        <div class="card">
                            <div class="error-box">
                                <div class="error-icon">❌</div>
                                <div class="error-title">Failed to Find Clean Proxy</div>
                                <div class="error-message">${data.error}</div>
                                ${data.attempts ? `<div class="attempts-info">Tested ${data.attempts} proxies, none passed all checks.</div>` : ''}
                                ${failInfo}
                            </div>
                        </div>
                    `;
                } else {
                    // Determine distance badge
                    let distanceBadge = '';
                    if (data.distance <= 2) {
                        distanceBadge = '<span class="distance-badge excellent">🏆 EXCELLENT - Within 2 miles</span>';
                    } else if (data.distance < 5) {
                        distanceBadge = '<span class="distance-badge good">✅ GREAT - Within 5 miles</span>';
                    } else if (data.distance <= 10) {
                        distanceBadge = '<span class="distance-badge decent">👍 GOOD - Within 10 miles</span>';
                    } else {
                        distanceBadge = '<span class="distance-badge decent">📍 ' + data.distance.toFixed(1) + ' miles away</span>';
                    }
                    
                    resultsDiv.innerHTML = `
                        <div class="card">
                            <div class="success-box">
                                <div class="success-icon">✅</div>
                                <div class="success-title">Clean Proxy Found!</div>
                                <div class="success-subtitle">Provider: <strong>${data.provider || 'SOAX'}</strong> • Passed all detection checks</div>
                                ${distanceBadge}
                                
                                <div class="proxy-output" onclick="copyToClipboard('${data.full_string}', this)">
                                    <div class="proxy-output-label">📋 Full Proxy String (click to copy)</div>
                                    <div class="proxy-output-value">${data.full_string}</div>
                                    <div class="copy-hint">Click to copy</div>
                                </div>
                                
                                <div class="proxy-output parsed-output">
                                    <div class="proxy-output-label">🔐 Parsed Details (click each to copy)</div>
                                    <div class="parsed-row"><strong>Server:</strong> <span class="parsed-value" onclick="event.stopPropagation(); copyToClipboard('${data.server}', this)">${data.server}</span></div>
                                    <div class="parsed-row"><strong>Port:</strong> <span class="parsed-value" onclick="event.stopPropagation(); copyToClipboard('${data.port}', this)">${data.port}</span></div>
                                    <div class="parsed-row"><strong>Username:</strong> <span class="parsed-value" onclick="event.stopPropagation(); copyToClipboard('${data.username}', this)">${data.username}</span></div>
                                    <div class="parsed-row"><strong>Password:</strong> <span class="parsed-value" onclick="event.stopPropagation(); copyToClipboard('${data.password}', this)">${data.password}</span></div>
                                </div>
                                
                                <div class="location-info">
                                    <h4>📍 Proxy Location</h4>
                                    <div class="location-row"><strong>City:</strong> ${data.city}</div>
                                    <div class="location-row"><strong>Region:</strong> ${data.region}</div>
                                    <div class="location-row"><strong>Country:</strong> ${data.country}</div>
                                    <div class="location-row"><strong>Distance:</strong> ${data.distance.toFixed(1)} miles from target</div>
                                </div>
                                
                                <div class="isp-info">
                                    <h4>🌐 ISP Information</h4>
                                    <div class="location-row"><strong>ISP:</strong> ${data.isp}</div>
                                    <div class="location-row"><strong>Network:</strong> ${data.as_name || 'Unknown'}</div>
                                    <div class="location-row"><strong>Usage Type:</strong> ${data.usage_type || '-'}</div>
                                </div>
                                
                                <div class="detection-info">
                                    <h4>🔍 Detection Status (IP2Location)</h4>
                                    <div class="detection-grid">
                                        <div class="detection-item ${data.is_proxy ? 'bad' : 'good'}">
                                            <span class="detection-label">Proxy Detected:</span>
                                            <span class="detection-value">${data.is_proxy ? '🚨 YES' : '✅ NO'} (raw: ${data.is_proxy_raw || 'N/A'})</span>
                                        </div>
                                        <div class="detection-item ${data.is_vpn ? 'bad' : 'good'}">
                                            <span class="detection-label">VPN:</span>
                                            <span class="detection-value">${data.is_vpn ? '🚨 YES' : '✅ NO'}</span>
                                        </div>
                                        <div class="detection-item ${data.is_datacenter ? 'bad' : 'good'}">
                                            <span class="detection-label">Datacenter:</span>
                                            <span class="detection-value">${data.is_datacenter ? '🚨 YES' : '✅ NO'}</span>
                                        </div>
                                        <div class="detection-item">
                                            <span class="detection-label">Proxy Type:</span>
                                            <span class="detection-value">${data.proxy_type || '-'}</span>
                                        </div>
                                        <div class="detection-item ${data.fraud_score > 50 ? 'bad' : 'good'}">
                                            <span class="detection-label">Fraud Score:</span>
                                            <span class="detection-value">${data.fraud_score}</span>
                                        </div>
                                        <div class="detection-item">
                                            <span class="detection-label">Residential:</span>
                                            <span class="detection-value">${data.is_residential ? 'YES' : 'NO'}</span>
                                        </div>
                                    </div>
                                </div>
                                
                                <div class="stats-grid">
                                    <div class="stat-item">
                                        <div class="stat-label">Exit IP</div>
                                        <div class="stat-value">${data.ip}</div>
                                    </div>
                                    <div class="stat-item">
                                        <div class="stat-label">Attempts Used</div>
                                        <div class="stat-value">${data.attempts_used}</div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    `;
                }
            } catch (e) {
                let errorMsg = e.message;
                if (e.name === 'AbortError') {
                    errorMsg = 'Request timed out after 30 seconds. Try again.';
                }
                resultsDiv.innerHTML = `
                    <div class="card">
                        <div class="error-box">
                            <div class="error-icon">⚠️</div>
                            <div class="error-title">Connection Error</div>
                            <div class="error-message">${errorMsg}</div>
                        </div>
                    </div>
                `;
            }
            
            btn.disabled = false;
            btn.textContent = '🔍 Find Clean Proxy';
        }
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/config-status', methods=['GET'])
def config_status():
    """Check if system is configured"""
    config = get_env_config()
    
    has_soax = bool(config['soax_package_id'] and config['soax_password'])
    
    return jsonify({
        'app_version': APP_VERSION,
        'has_ipapi': bool(config['ipapi_key']),
        'has_soax': has_soax,
        'ready': bool(config['ipapi_key'] and has_soax)
    })


@app.route('/version', methods=['GET'])
def get_version():
    """Get current app version"""
    return jsonify({
        'app_version': APP_VERSION
    })


@app.route('/generate', methods=['POST'])
def generate():
    data = request.json
    target_address = data.get('target_address', '')
    mapbox_key = data.get('mapbox_key', '')
    max_distance = data.get('max_distance', 5)
    max_attempts = data.get('max_attempts', 10)
    
    config = get_env_config()
    
    if not config['ipapi_key']:
        return jsonify({'error': 'ip-api.com API key not configured'})
    
    if not config['soax_package_id'] or not config['soax_password']:
        return jsonify({'error': 'SOAX credentials not configured'})
    
    if not mapbox_key:
        return jsonify({'error': 'Mapbox API key required'})
    
    if not target_address:
        return jsonify({'error': 'Target address required'})
    
    # Geocode address
    location = geocode_address(target_address, mapbox_key)
    if not location:
        return jsonify({'error': 'Could not geocode address. Please check the address format.'})
    
    # Extract location for proxy targeting
    city = location.get('city', '')
    region = location.get('region', '')
    
    # Helper function to test a single proxy (runs in thread)
    def test_single_proxy(proxy):
        result = test_proxy(
            proxy['full_string'],
            location['lat'],
            location['lon'],
            config['ipapi_key'],
            max_distance
        )
        return proxy, result
    
    # Generate all SOAX proxies
    all_proxies = []
    for i in range(max_attempts):
        proxy = build_soax_proxy(
            package_id=config['soax_package_id'],
            password=config['soax_password'],
            country='us',
            region=region,
            city=city,
            session_length=3600
        )
        all_proxies.append(proxy)
    
    # Test proxies in parallel (5 at a time)
    batch_size = 5
    last_fail_reasons = []
    last_result = None
    total_tested = 0
    
    for batch_start in range(0, len(all_proxies), batch_size):
        batch = all_proxies[batch_start:batch_start + batch_size]
        
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = {executor.submit(test_single_proxy, proxy): proxy for proxy in batch}
            
            for future in as_completed(futures):
                total_tested += 1
                try:
                    proxy, result = future.result()
                    last_result = result
                    
                    if not result['success']:
                        last_fail_reasons = [result.get('error', 'Connection failed')]
                        continue
                    
                    last_fail_reasons = result.get('fail_reasons', [])
                    
                    if result['passed']:
                        # FOUND A GOOD PROXY!
                        return jsonify({
                            'success': True,
                            'provider': 'SOAX',
                            'full_string': proxy['full_string'],
                            'server': proxy['server'],
                            'port': proxy['port'],
                            'username': proxy['username'],
                            'password': proxy['password'],
                            'session_id': proxy['session_id'],
                            'ip': result['ip'],
                            'city': result['city'],
                            'region': result['region'],
                            'country': result['country'],
                            'lat': result.get('lat'),
                            'lon': result.get('lon'),
                            'distance': result['distance'],
                            'isp': result['isp'],
                            'as_name': result.get('as_name', 'Unknown'),
                            'attempts_used': total_tested,
                            'target_address': target_address,
                            'target_location': location['place_name']
                        })
                except Exception as e:
                    last_fail_reasons = [f"Error: {str(e)}"]
    
    # No proxy found
    return jsonify({
        'error': f'Could not find a proxy within {max_distance} miles after {total_tested} attempts.',
        'attempts': total_tested,
        'last_fail_reasons': last_fail_reasons,
        'last_result': {
            'ip': last_result.get('ip') if last_result else None,
            'city': last_result.get('city') if last_result else None,
            'region': last_result.get('region') if last_result else None,
            'isp': last_result.get('isp') if last_result else None,
            'distance': last_result.get('distance') if last_result else None,
        } if last_result else None
    })


@app.route('/debug-env', methods=['GET'])
def debug_env():
    """Debug endpoint - shows what environment variables are set"""
    config = get_env_config()
    
    return jsonify({
        'has_ipapi': bool(config['ipapi_key']),
        'ipapi_key_preview': config['ipapi_key'][:6] + '...' if config['ipapi_key'] else 'NOT SET',
        'has_soax': bool(config['soax_package_id'] and config['soax_password']),
        'soax_package_id': config['soax_package_id'] or 'NOT SET',
        'soax_password_preview': config['soax_password'][:4] + '...' if config['soax_password'] else 'NOT SET',
    })


@app.route('/test-proxy', methods=['GET'])
def test_proxy_endpoint():
    """Quick test - try to connect through SOAX and get IP"""
    config = get_env_config()
    
    if not config['soax_package_id'] or not config['soax_password']:
        return jsonify({'error': 'SOAX not configured'})
    
    # Build a simple proxy
    proxy = build_soax_proxy(
        package_id=config['soax_package_id'],
        password=config['soax_password'],
        country='us',
        session_length=3600
    )
    
    proxy_url = f"http://{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"
    proxies = {"http": proxy_url, "https": proxy_url}
    
    try:
        response = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=10)
        ip = response.json()['ip']
        return jsonify({
            'success': True,
            'ip': ip,
            'proxy_used': proxy['full_string']
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'proxy_tried': proxy['full_string']
        })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
