from flask import Flask, render_template_string, request, jsonify
import re
import math
import requests
import os
import random
import string
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

# App version
APP_VERSION = '1.0.1'

# Environment Variables (set in Render):
# IPAPI_KEY - ip-api.com API key (for location/distance)
# SOAX_PACKAGE_ID - SOAX package ID
# SOAX_PASSWORD - SOAX password


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
    """Convert address to coordinates using Mapbox"""
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
        
        context = {item['id'].split('.')[0]: item['text'] for item in feature.get('context', [])}
        
        return {
            'lat': coords[1],
            'lon': coords[0],
            'place_name': feature.get('place_name', ''),
            'city': feature.get('text', '') or context.get('place', ''),
            'region': context.get('region', ''),
            'country': context.get('country', 'United States')
        }
    except Exception as e:
        return None


def build_soax_proxy(package_id, password, country='us', region=None, city=None, session_length=3600):
    """Build SOAX proxy string"""
    session_id = generate_session_id()
    
    username_parts = [f"package-{package_id}"]
    
    if country:
        username_parts.append(f"country-{country.lower()}")
    if region:
        region_clean = region.lower().replace(' ', '+')
        username_parts.append(f"region-{region_clean}")
    if city:
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
    R = 3959
    
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    
    a = math.sin(delta_lat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    return R * c


def test_proxy(proxy_string, target_lat, target_lon, ipapi_key, max_distance=5):
    """Test a proxy - ONLY check distance with ip-api (no detection)"""
    try:
        match = re.match(r'^(.+):(.+)@(.+):(\d+)$', proxy_string)
        if not match:
            return {'success': False, 'error': 'Invalid proxy format'}
        
        username = match.group(1)
        password = match.group(2)
        host = match.group(3)
        port = match.group(4)
        
        proxy_url = f"http://{username}:{password}@{host}:{port}"
        proxies = {"http": proxy_url, "https": proxy_url}
        
        # Get proxy exit IP
        proxy_ip = None
        try:
            ip_response = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=3)
            proxy_ip = ip_response.json()['ip']
        except:
            try:
                ip_response = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=3)
                proxy_ip = ip_response.json()['origin'].split(',')[0].strip()
            except Exception as e:
                return {'success': False, 'error': 'Connection timeout'}
        
        if not proxy_ip:
            return {'success': False, 'error': 'Could not get proxy IP'}
        
        # Check location with ip-api
        try:
            ipapi_response = requests.get(
                f"https://pro.ip-api.com/json/{proxy_ip}?key={ipapi_key}&fields=status,message,country,regionName,city,lat,lon,isp,org,as,mobile",
                timeout=3
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
        
        # Calculate distance
        distance = haversine_distance(target_lat, target_lon, lat, lon)
        
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
        
        # ALL CHECKS PASSED
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
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
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
            margin-bottom: 10px;
            font-size: 28px;
            background: linear-gradient(90deg, #00d9ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .subtitle {
            text-align: center;
            color: #888;
            margin-bottom: 30px;
            font-size: 14px;
        }
        
        .badge {
            display: inline-block;
            background: #ff6b6b;
            color: white;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: bold;
            margin-left: 10px;
        }
        
        .card {
            background: rgba(255,255,255,0.05);
            border-radius: 16px;
            padding: 25px;
            margin-bottom: 20px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        label {
            display: block;
            margin-bottom: 8px;
            color: #aaa;
            font-size: 14px;
        }
        
        input, select {
            width: 100%;
            padding: 14px;
            border: 2px solid rgba(255,255,255,0.1);
            border-radius: 10px;
            background: rgba(0,0,0,0.3);
            color: #fff;
            font-size: 16px;
            transition: border-color 0.3s;
        }
        
        input:focus, select:focus {
            outline: none;
            border-color: #00d9ff;
        }
        
        .row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }
        
        button {
            width: 100%;
            padding: 16px;
            background: linear-gradient(90deg, #00d9ff, #00ff88);
            border: none;
            border-radius: 10px;
            color: #1a1a2e;
            font-size: 18px;
            font-weight: bold;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(0,217,255,0.3);
        }
        
        button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }
        
        .config-status {
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
        }
        
        .config-status.success {
            background: rgba(0,255,136,0.1);
            border: 1px solid rgba(0,255,136,0.3);
            color: #00ff88;
        }
        
        .config-status.error {
            background: rgba(255,107,107,0.1);
            border: 1px solid rgba(255,107,107,0.3);
            color: #ff6b6b;
        }
        
        .success-box {
            text-align: center;
            padding: 20px;
        }
        
        .success-icon {
            font-size: 48px;
            margin-bottom: 10px;
        }
        
        .success-title {
            font-size: 24px;
            font-weight: bold;
            color: #00ff88;
            margin-bottom: 5px;
        }
        
        .success-subtitle {
            color: #888;
            margin-bottom: 20px;
        }
        
        .proxy-output {
            background: #0a0a1a;
            border: 2px solid #00d9ff;
            border-radius: 10px;
            padding: 15px;
            margin: 15px 0;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .proxy-output:hover {
            background: #1a1a3a;
        }
        
        .proxy-output-label {
            font-size: 12px;
            color: #00d9ff;
            margin-bottom: 8px;
        }
        
        .proxy-output-value {
            font-family: 'Courier New', monospace;
            font-size: 13px;
            word-break: break-all;
            color: #fff;
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
        
        .parsed-value.copied {
            background: #1a3a1a;
            border-color: #00ff88;
        }
        
        .copy-hint {
            font-size: 11px;
            color: #666;
            margin-top: 8px;
        }
        
        .location-info {
            background: rgba(0,0,0,0.2);
            border-radius: 10px;
            padding: 15px;
            margin-top: 20px;
            text-align: left;
        }
        
        .location-info h4 {
            color: #00d9ff;
            margin-bottom: 10px;
        }
        
        .location-row {
            margin: 5px 0;
            color: #aaa;
        }
        
        .location-row strong {
            color: #fff;
        }
        
        .distance-badge {
            display: inline-block;
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: bold;
            margin: 10px 0;
        }
        
        .distance-excellent {
            background: rgba(0,255,136,0.2);
            color: #00ff88;
        }
        
        .distance-good {
            background: rgba(0,217,255,0.2);
            color: #00d9ff;
        }
        
        .error-box {
            text-align: center;
            padding: 20px;
        }
        
        .error-icon {
            font-size: 48px;
            margin-bottom: 10px;
        }
        
        .error-title {
            font-size: 20px;
            color: #ff6b6b;
            margin-bottom: 10px;
        }
        
        .error-message {
            color: #aaa;
        }
        
        .fail-reasons {
            background: rgba(255,107,107,0.1);
            border-radius: 8px;
            padding: 15px;
            margin-top: 15px;
            text-align: left;
        }
        
        .fail-reasons ul {
            margin: 10px 0 0 20px;
            color: #ff6b6b;
        }
        
        .loading {
            text-align: center;
            padding: 40px;
        }
        
        .spinner {
            width: 50px;
            height: 50px;
            border: 4px solid rgba(0,217,255,0.2);
            border-top-color: #00d9ff;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🌐 Proxy Generator <span class="badge">K</span></h1>
        <p class="subtitle">Distance-only checking • Fast parallel testing</p>
        
        <div class="card">
            <div id="configStatus" class="config-status">Checking configuration...</div>
            
            <div class="form-group">
                <label>Mapbox API Key</label>
                <input type="text" id="mapboxKey" placeholder="pk.eyJ1Ijoi..." />
            </div>
            
            <div class="form-group">
                <label>Target Address</label>
                <input type="text" id="targetAddress" placeholder="123 Main St, Miami, FL" />
            </div>
            
            <div class="row">
                <div class="form-group">
                    <label>Max Distance</label>
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
            
            <button id="generateBtn" onclick="generateProxy()">🔍 Find Proxy</button>
        </div>
        
        <div id="results"></div>
    </div>
    
    <script>
        const CLIENT_APP_VERSION = '1.0.1';
        let configReady = false;
        
        // Load saved Mapbox key
        const savedKey = localStorage.getItem('mapbox_key');
        if (savedKey) {
            document.getElementById('mapboxKey').value = savedKey;
        }
        
        // Save Mapbox key on change
        document.getElementById('mapboxKey').addEventListener('change', function() {
            localStorage.setItem('mapbox_key', this.value);
        });
        
        // Check configuration on load
        window.onload = async function() {
            try {
                const response = await fetch('/config-status');
                const config = await response.json();
                
                const statusDiv = document.getElementById('configStatus');
                
                if (config.ready) {
                    statusDiv.className = 'config-status success';
                    statusDiv.innerHTML = '✅ <strong>System Ready</strong> — SOAX + ip-api configured (Distance-only mode)';
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
                element.classList.add('copied');
                const original = element.innerHTML;
                
                if (element.classList.contains('parsed-value')) {
                    element.innerHTML = '✓ Copied!';
                    element.style.color = '#00ff88';
                    setTimeout(() => {
                        element.innerHTML = original;
                        element.style.color = '';
                        element.classList.remove('copied');
                    }, 1000);
                } else {
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
                alert('System not configured. Check environment variables in Render.');
                return;
            }
            
            localStorage.setItem('mapbox_key', mapboxKey);
            
            btn.disabled = true;
            btn.textContent = '⏳ Finding proxy...';
            
            resultsDiv.innerHTML = `
                <div class="card">
                    <div class="loading">
                        <div class="spinner"></div>
                        <p>Testing proxies in parallel...</p>
                        <p style="font-size: 12px; color: #666; margin-top: 10px;">Checking distance only (no detection)</p>
                    </div>
                </div>
            `;
            
            try {
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
                        failInfo = '<div class="fail-reasons"><strong>Last failure:</strong><ul>' + 
                            data.last_fail_reasons.map(r => '<li>' + r + '</li>').join('') + 
                            '</ul></div>';
                    }
                    resultsDiv.innerHTML = `
                        <div class="card">
                            <div class="error-box">
                                <div class="error-icon">❌</div>
                                <div class="error-title">${data.error}</div>
                                <div class="error-message">Tried ${data.attempts || 0} proxies</div>
                                ${failInfo}
                            </div>
                        </div>
                    `;
                } else {
                    let distanceBadge = '';
                    if (data.distance <= 2) {
                        distanceBadge = '<div class="distance-badge distance-excellent">📍 ' + data.distance.toFixed(1) + ' miles - EXCELLENT</div>';
                    } else if (data.distance <= 5) {
                        distanceBadge = '<div class="distance-badge distance-good">📍 ' + data.distance.toFixed(1) + ' miles - GREAT</div>';
                    } else {
                        distanceBadge = '<div class="distance-badge distance-good">📍 ' + data.distance.toFixed(1) + ' miles</div>';
                    }
                    
                    resultsDiv.innerHTML = `
                        <div class="card">
                            <div class="success-box">
                                <div class="success-icon">✅</div>
                                <div class="success-title">Proxy Found!</div>
                                <div class="success-subtitle">Within distance • Attempt ${data.attempts_used}</div>
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
                                    <div class="location-row"><strong>ISP:</strong> ${data.isp}</div>
                                    <div class="location-row"><strong>Distance:</strong> ${data.distance.toFixed(1)} miles</div>
                                    <div class="location-row"><strong>Exit IP:</strong> ${data.ip}</div>
                                </div>
                            </div>
                        </div>
                    `;
                }
            } catch (e) {
                let errorMsg = e.message;
                if (e.name === 'AbortError') {
                    errorMsg = 'Request timed out. Try again.';
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
            btn.textContent = '🔍 Find Proxy';
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
    config = get_env_config()
    
    has_soax = bool(config['soax_package_id'] and config['soax_password'])
    
    return jsonify({
        'app_version': APP_VERSION,
        'has_ipapi': bool(config['ipapi_key']),
        'has_soax': has_soax,
        'ready': bool(config['ipapi_key'] and has_soax)
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
    
    city = location.get('city', '')
    region = location.get('region', '')
    
    # Helper function to test a single proxy
    def test_single_proxy(proxy):
        result = test_proxy(
            proxy['full_string'],
            location['lat'],
            location['lon'],
            config['ipapi_key'],
            max_distance
        )
        return proxy, result
    
    # Generate all proxies upfront
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
                            'distance': result['distance'],
                            'isp': result['isp'],
                            'attempts_used': total_tested,
                            'target_address': target_address
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
            'distance': last_result.get('distance') if last_result else None,
        } if last_result else None
    })


@app.route('/debug-env', methods=['GET'])
def debug_env():
    """Debug endpoint to check environment variables"""
    config = get_env_config()
    return jsonify({
        'has_ipapi': bool(config['ipapi_key']),
        'ipapi_key_preview': config['ipapi_key'][:6] + '...' if config['ipapi_key'] else 'NOT SET',
        'has_soax_package': bool(config['soax_package_id']),
        'soax_package_id': config['soax_package_id'] or 'NOT SET',
        'has_soax_password': bool(config['soax_password']),
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
