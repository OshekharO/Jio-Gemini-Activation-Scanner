# Firebase Tutorial

A defensive discovery and authorized security testing guide for Firebase Realtime Database configurations. This document complements the **Jio Gemini Activation Scanner** project.

---

> ## ⚠️ AUTHORIZATION REQUIRED
>
> Only inspect Firebase projects, websites, applications, repositories, and infrastructure that **you own** or have **explicit written permission** to assess.
>
> Do **not** use this guide to discover, enumerate, dump, modify, or collect data from third party Firebase databases. Unauthorized access violates the CFAA (United States), the Computer Misuse Act (United Kingdom), the IT Act (India), and Firebase Terms of Service.

---

## 📖 Why This Guide Exists

The main scanner file `Jio.py` reads Firebase Realtime Database panels listed in `panels.txt` and interacts with nodes such as `/clients` and `/messages/{device_id}`. That workflow depends on **legitimate access** to Firebase databases, either your own test instances or databases you have written authorization to use.

Because Firebase client configurations are commonly embedded in web bundles, mobile apps, and Git history, this document explains **how to find, verify, and audit them responsibly** during authorized assessments.

> This is a **defensive companion** document. It is **not** a recipe for harvesting other people's endpoints.

---

## 🎯 What This Guide Covers

| Area | In Scope |
|------|:--------:|
| Website and JavaScript bundle discovery | ✅ |
| Android APK inspection | ✅ |
| iOS plist inspection | ✅ |
| Git repository and history audit | ✅ |
| Firebase Console verification | ✅ |
| Security Rules review | ✅ |
| Safe read and write testing | ✅ |
| Remediation guidance | ✅ |
| Third party panel harvesting | ❌ |

---

## 1. Understand Firebase Realtime Database URLs

Firebase supports two hostname formats:

| Format | Example |
|--------|---------|
| Legacy | `https://PROJECT_ID.firebaseio.com` |
| Regional | `https://DATABASE_NAME.REGION.firebasedatabase.app` |

The REST API appends `.json` to any path:

```
https://YOUR_DATABASE_URL/.json
https://YOUR_DATABASE_URL/clients.json
```

> **Key point:** Access is **always** governed by Security Rules. A resolvable URL does **not** imply open access.

---

## 2. Find Firebase Config in Websites You Own

### Step 1 — Open DevTools

Press `F12`, then open:

- **Sources** tab
- **Network** tab
- **Application** tab

### Step 2 — Search Loaded JavaScript Files

Use global search with `Ctrl+Shift+F` and look for:

| Keyword | Purpose |
|---------|---------|
| `firebaseConfig` | Config object name |
| `firebase.initializeApp` | SDK init call |
| `initializeApp` | Short init call |
| `databaseURL` | Realtime DB endpoint |
| `projectId` | Firebase project ID |
| `firebaseio.com` | Legacy DB host |
| `firebasedatabase.app` | Regional DB host |
| `apiKey` | Web API key |
| `appId` | Firebase app ID |
| `storageBucket` | Cloud Storage bucket |
| `messagingSenderId` | FCM sender ID |

### Step 3 — Typical Config Block

```js
const firebaseConfig = {
  apiKey: "...",
  authDomain: "example.firebaseapp.com",
  databaseURL: "https://example-default-rtdb.firebaseio.com",
  projectId: "example",
  appId: "..."
};
```

> The value that matters for Realtime Database work is **`databaseURL`**.

**Common locations to check:**

- `main.js`
- `app.js`
- `firebase-config.js`
- `environment.ts`
- `config.js`
- Any code split chunk under `/static/js/`

---

## 3. Search Local Website Source Trees

| Tool | Command |
|------|---------|
| **grep** (Linux/macOS) | `grep -RniE 'firebaseio\.com\|firebasedatabase\.app\|databaseURL\|firebaseConfig\|initializeApp' ./src` |
| **git grep** | `git grep -nE 'firebaseio\.com\|firebasedatabase\.app\|databaseURL\|firebaseConfig'` |
| **ripgrep** (fastest) | `rg -n 'firebaseio\.com\|firebasedatabase\.app\|databaseURL\|firebaseConfig\|initializeApp' .` |

**Files worth inspecting first:**

```
.env
.env.local
.env.production
firebase.json
.firebaserc
src/
public/
config/
assets/
```

---

## 4. Android Application Inspection (Authorized APKs Only)

### Step 1 — Obtain the APK Legitimately

Use your own build pipeline output, a test build, or an APK for which you have **explicit testing authorization**.

### Step 2 — Inspect the APK as an Archive

```bash
unzip -l application.apk
```

Look for:

- `google-services.json`
- `resources.arsc`
- `res/`
- `assets/`

You can also open it in **Android Studio → Build → Analyze APK**.

### Step 3 — Extract and Search

```bash
unzip application.apk -d apk-extracted

rg -n 'firebaseio\.com|firebasedatabase\.app|google_app_id|project_id|firebase_database_url' apk-extracted/
```

**Typical Firebase Android config keys:**

| Key | Description |
|-----|-------------|
| `project_id` | Firebase project identifier |
| `google_app_id` | Android app identifier |
| `firebase_database_url` | Realtime DB endpoint |
| `gcm_defaultSenderId` | FCM sender ID |
| `google_api_key` | Google API key |

> **Reminder:** A client side `apiKey` is **not** a database credential. Realtime Database access is enforced by Security Rules, not by the presence of a client key.

---

## 5. iOS Application Inspection (Authorized Builds Only)

Look for the file:

```
GoogleService-Info.plist
```

**Typical keys:**

| Key | Description |
|-----|-------------|
| `PROJECT_ID` | Firebase project identifier |
| `GOOGLE_APP_ID` | iOS app identifier |
| `API_KEY` | Google API key |
| `DATABASE_URL` | Realtime DB endpoint |
| `STORAGE_BUCKET` | Cloud Storage bucket |

Search an extracted test package:

```bash
rg -n 'firebaseio\.com|firebasedatabase\.app|DATABASE_URL|GOOGLE_APP_ID' ./authorized-ios-app/
```

> The `GoogleService-Info.plist` file can also be found inside the `.app` bundle. It is part of the app resources, not the binary.

---

## 6. Git Repository Discovery (Repositories You Control)

### Search the Current Working Tree

```bash
git grep -n -E 'firebaseio\.com|firebasedatabase\.app|databaseURL|firebaseConfig'
```

### Find Common Config Files

```bash
find . \( -name 'google-services.json' \
      -o -name 'GoogleService-Info.plist' \
      -o -name 'firebase.json' \
      -o -name '.firebaserc' \)
```

### Search Historical Commits

Keys may have been removed but remain in history:

```bash
git log -S'firebaseio.com' --all
git log -S'databaseURL' --all
git log --all --oneline -- google-services.json
```

> **If a secret was committed:** rotate and revoke it in the Firebase Console and Google Cloud Console. Deleting the file from the working tree is **not** enough — Git history retains it.

---

## 7. Verify Your Own Endpoint (Read Only)

```bash
curl -i 'https://YOUR_DATABASE_URL/.json'
```

| Status | Meaning |
|--------|---------|
| `200 OK` | Rules permit the read |
| `401 Unauthorized` | Rejected by authentication or Security Rules |
| `404 Not Found` | Path or database does not exist |
| `403 Forbidden` | Rules explicitly deny access |

> A `200` response alone does **not** prove misconfiguration. It may be an intentionally public node.

---

## 8. Use a Harmless Test Node

> **Never** download production datasets to prove access.

Create a disposable location:

```
/security-audit-test
```

Seed it from your own Firebase Console:

```json
{
  "security-audit-test": {
    "status": "ok",
    "owner": "your-team"
  }
}
```

Then probe **only that path**:

```bash
curl -i 'https://YOUR_DATABASE_URL/security-audit-test.json'
```

---

## 9. Safe Write Testing

Only against an explicitly authorized test location:

```bash
curl -X PUT \
  -H 'Content-Type: application/json' \
  -d '{"test":true,"ts":"2026-09-30T00:00:00Z"}' \
  'https://YOUR_DATABASE_URL/security-audit-test.json'
```

Then clean up:

```bash
curl -X DELETE 'https://YOUR_DATABASE_URL/security-audit-test.json'
```

> The Firebase REST API supports `PUT`, `POST`, `PATCH`, and `DELETE`. Any write against production data is **out of scope** for a defensive audit.

---

## 10. Review Realtime Database Security Rules

**This is the actual security boundary.**

**Core rule types:**

| Rule | Purpose |
|------|---------|
| `.read` | Controls read access |
| `.write` | Controls write access |
| `.validate` | Validates data structure |
| `.indexOn` | Improves query performance |

### Restrictive Example (Per User Access)

```json
{
  "rules": {
    "users": {
      "$uid": {
        ".read": "auth != null && auth.uid === $uid",
        ".write": "auth != null && auth.uid === $uid"
      }
    }
  }
}
```

### Dangerous Patterns to Flag

```json
{ "rules": { ".read": true, ".write": true } }
```

```json
{ "rules": { ".read": true } }
```

> `.read` and `.write` **cascade to all descendants**. A permissive parent rule exposes the **entire subtree**.

---

## 11. Authentication Boundary Matrix

For every sensitive path, compare three access levels:

| Path | Anonymous | Auth User | Admin |
|------|:---------:|:---------:|:-----:|
| `/public` | ✅ / ❌ | ✅ / ❌ | ✅ |
| `/users/<uid>` | ❌ | Own UID only | ✅ |
| `/admin` | ❌ | ❌ | ✅ |
| `/messages/<device_id>` | ❌ | Owner only | ✅ |

> Document results **without extracting** unrelated production data.

---

## 12. Map Database Paths from Application Code

Search authorized source for SDK usage patterns:

| Function | Purpose |
|----------|---------|
| `getDatabase()` | Initialize DB instance |
| `getReference()` | Get root reference |
| `ref()` | Create reference to path |
| `child()` | Reference child node |
| `onValue()` | Listen for value changes |
| `on('value', ...)` | Listen for value changes (v8) |
| `once()` | Read value one time |
| `set()` | Overwrite data |
| `update()` | Merge data |
| `push()` | Add to list |
| `remove()` | Delete data |

**Example:**

```js
const db = getDatabase();
const usersRef = ref(db, "users");
const msgRef = ref(db, `messages/${deviceId}`);
```

This reveals intended paths such as `/users` and `/messages/{device_id}` — the same nodes the scanner reads.

> Treat them as **application metadata**, not as permission to access them from unauthorized clients.

---

## 13. Map Paths from Network Traffic (Your Own App)

| Step | Action |
|------|--------|
| 1 | Open DevTools → **Network** tab |
| 2 | Reload your application |
| 3 | Filter for `firebase` |
| 4 | Inspect requests to `*.firebaseio.com` or `*.firebasedatabase.app` |
| 5 | Record request paths and match against your Security Rules |

> Do **not** replay captured requests against systems outside your authorization scope.

---

## 14. Firebase Console Verification (Project Owners)

| Step | Action |
|------|--------|
| 1 | Open [Firebase Console](https://console.firebase.google.com/) |
| 2 | Select your project |
| 3 | Open **Build → Realtime Database** |
| 4 | Note the **Database URL** |
| 5 | Review the **Data** tab for unexpected nodes |
| 6 | Review the **Rules** tab |
| 7 | Review **Authentication** providers |
| 8 | Distinguish production and development databases |
| 9 | Remove unused databases, API keys, and service accounts |

---

## 15. Safe Assessment Checklist

```
[ ] Confirm written authorization is on file
[ ] Identify the Firebase project
[ ] Confirm the Realtime Database URL
[ ] Inventory client applications
[ ] Inspect website config (authorized domains only)
[ ] Inspect authorized Android builds
[ ] Inspect authorized iOS builds
[ ] Audit authorized Git repositories and history
[ ] Review Security Rules
[ ] Test anonymous read on a disposable node
[ ] Test authenticated read
[ ] Test authorization boundaries (own UID vs another UID)
[ ] Test write only on a disposable node
[ ] Document sensitive data exposure with minimal evidence
[ ] Rotate exposed credentials
[ ] Remove test data
[ ] Deliver remediation write up
```

---

## 16. What Counts as a Finding

| Finding | Example |
|---------|---------|
| **Public read** | Anonymous client reads a sensitive path such as `/users` |
| **Public write** | Anonymous client modifies a sensitive path such as `/config` |
| **Broken authorization** | User A reads `/users/UserB` |
| **Privilege escalation** | A normal user reaches `/admin` |
| **Sensitive data** | Tokens, OTPs, phone numbers, messages, or PII |

> Collect the **minimum** evidence required to demonstrate the issue, then stop.

---

## 17. Remediation

**Enforce per user access with Security Rules:**

```json
{
  "rules": {
    "users": {
      "$uid": {
        ".read": "auth != null && auth.uid === $uid",
        ".write": "auth != null && auth.uid === $uid"
      }
    }
  }
}
```

**Add validation:**

```json
{
  "rules": {
    "profiles": {
      "$uid": {
        ".validate": "newData.hasChildren(['displayName'])"
      }
    }
  }
}
```

**Additional hardening steps:**

| Step | Action |
|------|--------|
| 1 | Enable **Firebase App Check** to block unauthorized clients |
| 2 | Restrict Web API keys by HTTP referrer or app bundle ID in **Google Cloud Console → APIs & Services → Credentials** |
| 3 | Enable **Authentication** and require `auth != null` on any non public path |
| 4 | Use **Cloud Functions** for privileged operations instead of exposing admin paths to clients |

---

## 18. API Key Versus Database Authorization

A Firebase web config contains many values:

```
apiKey
authDomain
projectId
databaseURL
storageBucket
appId
messagingSenderId
```

> Finding these in a public bundle is **not a vulnerability by itself**.

**The question that matters is:**

> Who can read or write which database paths?

That answer lives **entirely** in Security Rules.

```
Firebase config exposed  ≠  Database compromised
```

---

## 19. Recommended File Layout for This Repository

```
Jio-Gemini-Activation-Scanner/
├── README.md              # Project overview (already present)
├── FIREBASE.md            # This defensive guide
├── Jio.py                 # Scanner (already present)
├── panels.txt             # Authorized panel list (already present)
└── .gitignore             # Should exclude secrets and results
```

**Suggested `.gitignore` additions:**

```
gemini_results.csv
gemini_activation_links.txt
*.log
.env
.env.*
google-services.json
GoogleService-Info.plist
```

---

## 20. How the Scanner Fits In

The scanner reads `/clients` and `/messages/{device_id}` from panels listed in `panels.txt`. This guide explains:

1. **How** those Firebase URLs are typically embedded in client code
2. **How** to safely verify a database endpoint you are authorized to test
3. **How** to review the Security Rules that determine whether the scanner's reads will succeed

> The `panels.txt` file should contain **only** Firebase databases that you own or have explicit authorization to use. If you obtained a URL from a public source without authorization, **remove it**.

**Format:**

```txt
# Comments are allowed and start with a hash symbol

# With API key
https://your-project.firebaseio.com|AIzaSyYourWebApiKey

# Without API key
https://your-project-default-rtdb.firebasedatabase.app|
```

---

## 21. References

| Resource | Link |
|----------|------|
| Firebase Realtime Database REST API | https://firebase.google.com/docs/reference/rest/database |
| Firebase REST API setup | https://firebase.google.com/docs/database/rest/start |
| Realtime Database Security Rules | https://firebase.google.com/docs/database/security |
| Security Rules reference | https://firebase.google.com/docs/reference/security/database |
| Manage rules via REST | https://firebase.google.com/docs/database/rest/app-management |
| Firebase App Check | https://firebase.google.com/docs/app-check |
| OWASP Mobile Security Testing Guide | https://owasp.org/www-project-mobile-security-testing-guide/ |
| Google Cloud API key best practices | https://cloud.google.com/docs/authentication/api-keys |

---

## 22. Responsible Use

### ✅ Intended Audience

- Application developers
- Security engineers
- Authorized penetration testers
- Bug bounty researchers operating within program scope
- Defensive security teams
- Firebase project owners

### ❌ Not Permitted

- Enumerating third party Firebase databases from public code
- Collecting personal information from databases you do not own
- Bypassing authentication or Security Rules on systems outside your scope
- Modifying data without explicit authorization

> **Core principle:** A Firebase configuration being visible in a client does **not** establish unauthorized access. **Security Rules** determine what is permitted. Test only what you own or are authorized to assess.

---

## 📄 License and Attribution

This document complements the [Jio Gemini Activation Scanner](https://github.com/OshekharO/Jio-Gemini-Activation-Scanner) project and inherits its educational use only intent. See the main `README.md` for the project disclaimer.
