# Jio-Gemini-Activation-Scanner

Automated scanner for Jio Gemini (Google AI) activation links using exposed Firebase Realtime Database panels and Jio's public APIs.

## How it works

1. Loads Firebase panels from `panels.txt`
2. Scans `/clients` for online devices
3. Reads `/messages/{device_id}` to extract Jio mobile numbers
4. Sends OTP via Jio API, waits for OTP in Firebase, verifies it
5. Retrieves Google AI subscription activation link
6. Saves results to `gemini_results.csv` and links to `gemini_activation_links.txt`

## Requirements

- Python 3.10+
- `requests`

```bash
pip install requests
```

## Usage

```bash
# Run with all panels
python Jio.py

# Test with first 3 panels
python Jio.py --limit 3

# Skip first 20 panels
python Jio.py --skip 20

# Custom panels file
python Jio.py --panels my_panels.txt

# Adjust OTP timeout
python Jio.py --otp-timeout 30 --poll-interval 2
```

## CLI Arguments

| Flag | Default | Description |
|------|---------|-------------|
| `--panels` | `panels.txt` | Path to panels file |
| `--limit` | `0` (all) | Max panels to process |
| `--skip` | `0` | Skip first N panels |
| `--message-limit` | `100` | Messages per device to scan |
| `--otp-timeout` | `15` | OTP wait timeout (seconds) |
| `--poll-interval` | `1` | Firebase poll interval (seconds) |

## Output Files

- `gemini_activation_links.txt` — one activation URL per line
- `gemini_results.csv` — full results with status, device ID, mobile number, Nepal time

## panels.txt Format

```
https://panel-url.firebaseio.com|AIzaSyApiKeyHere
https://panel-url.firebaseio.com|
```

- `|` separates URL and API key
- API key is optional (empty after `|`)
- Lines starting with `#` are comments
- Supports both `firebaseio.com` and `firebasedatabase.app` URLs

## Special Thanks

Special thanks to **unknown** for the base code that this project is built upon.

## Disclaimer

This tool is for educational purposes only. Ensure you have permission to access any Firebase databases you scan.
