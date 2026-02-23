# Proxy Generator K

Distance-only proxy checker using SOAX + ip-api.

## Features
- ✅ Distance checking only (no IP2Location detection)
- ✅ Parallel proxy testing (5 at a time)
- ✅ SOAX geo-targeting (city/region)
- ✅ Mobile ISP filtering
- ✅ Flagged ISP filtering (RCN, Starlink)

## Environment Variables (set in Render)

| Variable | Description |
|----------|-------------|
| `IPAPI_KEY` | ip-api.com API key |
| `SOAX_PACKAGE_ID` | SOAX package ID |
| `SOAX_PASSWORD` | SOAX password |

## Deployment

1. Push to GitHub
2. Connect to Render
3. Set environment variables
4. Deploy
