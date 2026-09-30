# FIREBASE.md

A defensive discovery and authorized security-testing guide for Firebase Realtime Database configurations — complementing the [Jio-Gemini-Activation-Scanner](README.md) project.

> **⚠️ Authorization Required**
>
> Only inspect Firebase projects, websites, applications, repositories, and infrastructure that **you own** or have **explicit written permission** to assess.
>
> Do not use this guide to discover, enumerate, dump, modify, or collect data from third-party Firebase databases. Unauthorized access violates the CFAA (US), Computer Misuse Act (UK), IT Act (India), and Firebase's Terms of Service.

---

## 📰 Why This Guide Exists

The main scanner (`Jio.py`) reads Firebase Realtime Database panels listed in `panels.txt` and interacts with nodes such as `/clients` and `/messages/{device_id}`. That workflow depends on **legitimate access** to Firebase databases — either your own test instances or databases you have written authorization to use.

Because Firebase client configurations are commonly embedded in web bundles, mobile apps, and Git history, this document explains **how to find, verify, and audit them responsibly** during authorized assessments.

This is a **defensive companion** — not a recipe for harvesting other people's endpoints.

---

## 🎯 What This Guide Covers

| Area | Covered |
|---|---|
| Website / JS bundle discovery | ✅ |
| Android APK inspection | ✅ |
| iOS `.plist` inspection | ✅ |
| Git repository & history audit | ✅ |
| Firebase Console verification | ✅ |
| Security Rules review | ✅ |
| Safe read/write testing | ✅ |
| Remediation guidance | ✅ |
| Third-party panel harvesting | ❌ |

---

## 1. Understand Firebase Realtime Database URLs

Two hostname formats are supported by Firebase:

```
https://PROJECT_ID.firebaseio.com
https://DATABASE_NAME.REGION.firebasedatabase.app
```

The REST API appends `.json` to any path:

```
https://YOUR_DATABASE_URL/.json
https://YOUR_DATABASE_URL/clients.json
```

Access is **always** governed by Security Rules — a resolvable URL does not imply open access.

---

## 2. Find Firebase Config in Websites You Own

### Step 1 — Open DevTools

```
F12  →  Sources  →  Network  →  Application
```

### Step 2 — Search loaded JS

Use global search (`Ctrl+Shift+F`) for:

```
firebaseConfig
firebase.initializeApp
initializeApp
databaseURL
projectId
firebaseio.com
firebasedatabase.app
apiKey
appId
storageBucket
messagingSenderId
```

### Step 3 — Typical config block

```js
const firebaseConfig = {
  apiKey: "...",
  authDomain: "example.firebaseapp.com",
  databaseURL: "https://example-default-rtdb.firebaseio.com",
  projectId: "example",
  appId: "..."
};
```

The value that matters for Realtime Database work is `databaseURL`.

**Common locations to check:** `main.js`, `app.js`, `firebase-config.js`, `environment.ts`, `config.js`, and any code-split chunk under `/static/js/`.

---

## 3. Search Local Website Source Trees

### Linux / macOS

```bash
grep -RniE \
  'firebaseio\.com|firebasedatabase\.app|databaseURL|firebaseConfig|initializeApp' \
  ./src
```

### Git working tree

```bash
git grep -nE \
  'firebaseio\.com|firebasedatabase\.app|databaseURL|firebaseConfig'
```

### ripgrep (fastest)

```bash
rg -n \
  'firebaseio\.com|firebasedatabase\.app|databaseURL|firebaseConfig|initializeApp' \
  .
```

Files worth inspecting first:

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

### Step 1 — Obtain the APK legitimately

Use your own build pipeline output, a test build, or an APK for which you have **explicit testing authorization**.

### Step 2 — Inspect as an archive

```bash
unzip -l application.apk
```

Look for:

```
google-services.json
resources.arsc
res/
assets/
```

Or open it in **Android Studio → Build → Analyze APK**.

### Step 3 — Extract and search

```bash
unzip application.apk -d apk-extracted

rg -n \
  'firebaseio\.com|firebasedatabase\.app|google_app_id|project_id|firebase_database_url' \
  apk-extracted/
```

Typical Firebase Android config keys:

```
project_id
google_app_id
firebase_database_url
gcm_defaultSenderId
google_api_key
```

> **Reminder:** A client-side `apiKey` is not a database credential. Realtime Database access is enforced by Security Rules, not by the presence of a client key.

---

## 5. iOS Application Inspection (Authorized Builds Only)

Look for:

```
GoogleService-Info.plist
```

Typical keys:

```
PROJECT_ID
GOOGLE_APP_ID
API_KEY
DATABASE_URL
STORAGE_BUCKET
```

Search an extracted test package:

```bash
rg -n \
  'firebaseio\.com|firebasedatabase\.app|DATABASE_URL|GOOGLE_APP_ID' \
  ./authorized-ios-app/
```

`GoogleService-Info.plist` can also be found inside the `.app` bundle (it is part of the app resources, not the binary).

---

## 6. Git Repository Discovery (Repos You Control)

### Current working tree

```bash
git grep -n -E \
  'firebaseio\.com|firebasedatabase\.app|databaseURL|firebaseConfig'
```

### Common config files

```bash
find . \
  \( -name 'google-services.json' \
  -o -name 'GoogleService-Info.plist' \
  -o -name 'firebase.json' \
  -o -name '.firebaserc' \)
```

### Historical commits (keys may have been removed but remain in history)

```bash
git log -S'firebaseio.com' --all
git log -S'databaseURL' --all
git log --all --oneline -- google-services.json
```

> **If a secret was committed:** rotate/revoke it in Firebase Console and Google Cloud Console. Deleting the file from the working tree is not enough — Git history retains it.

---

## 7. Verify Your Own Endpoint (Read-Only)

Once you have identified a database belonging to your authorized Firebase project:

```bash
curl -i 'https://YOUR_DATABASE_URL/.json'
```

Possible responses:

| Status | Meaning |
|---|---|
| `200 OK` | Rules permit the read |
| `401 Unauthorized` | Rejected by auth or Security Rules |
| `404 Not Found` | Path or database does not exist |
| `403 Forbidden` | Rules explicitly deny access |

A `200` alone does **not** prove misconfiguration — it may be an intentionally public node.

---

## 8. Use a Harmless Test Node

Never download production datasets to "prove" access. Create a disposable location:

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

Then probe only that path:

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

The Firebase REST API supports `PUT`, `POST`, `PATCH`, and `DELETE`, but any write against production data is out of scope for a defensive audit.

---

## 10. Review Realtime Database Security Rules

This is the **actual security boundary**.

Core rule types:

```
.read
.write
.validate
.indexOn
```

### Restrictive example (per-user access)

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

### Dangerous patterns to flag

```json
{ "rules": { ".read": true, ".write": true } }
```

```json
{ "rules": { ".read": true } }
```

`.read` and `.write` **cascade to all descendants**, so a permissive parent rule exposes the entire subtree.

---

## 11. Authentication Boundary Matrix

For every sensitive path, compare three access levels:

| Path | Anonymous | Auth user | Admin |
|---|---|---|---|
| `/public` | ✅ / ❌ | ✅ / ❌ | ✅ |
| `/users/<uid>` | ❌ | own UID only | ✅ |
| `/admin` | ❌ | ❌ | ✅ |
| `/messages/<device_id>` | ❌ | owner only | ✅ |

Document results without extracting unrelated production data.

---

## 12. Map Database Paths from Application Code

Search authorized source for SDK usage patterns:

```js
getDatabase()
getReference()
ref()
child()
onValue()
on('value', ...)
once()
set()
update()
push()
remove()
```

Example:

```js
const db = getDatabase();
const usersRef = ref(db, "users");
const msgRef  = ref(db, `messages/${deviceId}`);
```

This reveals intended paths such as `/users` and `/messages/{device_id}` — the same nodes the scanner reads. Treat them as **application metadata**, not as permission to access them from unauthorized clients.

---

## 13. Map Paths from Network Traffic (Your Own App)

1. Open DevTools → **Network**.
2. Reload your application.
3. Filter for `firebase`.
4. Inspect requests to `*.firebaseio.com` or `*.firebasedatabase.app`.
5. Record request paths and match them against your Security Rules.

Do **not** replay captured requests against systems outside your authorization scope.

---

## 14. Firebase Console Verification (Project Owner)

1. Open [Firebase Console](https://console.firebase.google.com/).
2. Select your project.
3. **Build → Realtime Database**.
4. Note the **Database URL**.
5. Review the **Data** tab for unexpected nodes.
6. Review the **Rules** tab.
7. Review **Authentication** providers.
8. Distinguish production vs. development databases.
9. Remove unused databases, API keys, and service accounts.

---

## 15. Safe Assessment Checklist

```
[ ] Written authorization on file
[ ] Firebase project identified
[ ] Realtime Database URL confirmed
[ ] Client applications inventoried
[ ] Website config inspected (authorized domains only)
[ ] Authorized Android builds inspected
[ ] Authorized iOS builds inspected
[ ] Authorized Git repositories + history audited
[ ] Security Rules reviewed
[ ] Anonymous read test on disposable node
[ ] Authenticated read test
[ ] Authorization boundary tests (own UID vs. other UID)
[ ] Write test on disposable node only
[ ] Sensitive-data exposure documented (minimal evidence)
[ ] Exposed credentials rotated
[ ] Test data removed
[ ] Remediation write-up delivered
```

---

## 16. What Counts as a Finding?

| Finding | Example |
|---|---|
| Public read | Anonymous client reads `/users` |
| Public write | Anonymous client modifies `/config` |
| Broken authz | User A reads `/users/UserB` |
| Privilege escalation | Normal user reaches `/admin` |
| Sensitive data | Tokens, OTPs, phone numbers, messages, PII |

Collect the **minimum** evidence required to demonstrate the issue, then stop.

---

## 17. Remediation

Enforce per-user access with Security Rules:

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

Add validation:

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

Additional hardening:

- Enable **Firebase App Check** to block unauthorized clients.
- Restrict Web API keys by HTTP referrer / app bundle ID in **Google Cloud Console → APIs & Services → Credentials**.
- Enable **Authentication** and require `auth != null` on any non-public path.
- Use **Cloud Functions** for privileged operations instead of exposing admin paths to clients.

---

## 18. API Key ≠ Database Authorization

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

Finding these in a public bundle is **not** a vulnerability by itself. The question that matters is:

```
Who can read/write which database paths?
```

That answer lives entirely in Security Rules.

```
Firebase config exposed  ≠  Database compromised
```

---

## 19. Recommended File Layout for This Repository

```
Jio-Gemini-Activation-Scanner/
├── README.md              # Project overview (existing)
├── Firebase.md            # This guide (defensive)
├── Jio.py                 # Scanner (existing)
├── panels.txt             # Authorized panel list (existing)
└── .gitignore             # Should exclude secrets/results
```

Suggested `.gitignore` additions:

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

1. **How** those Firebase URLs are typically embedded in client code.
2. **How** to safely verify a database endpoint you are authorized to test.
3. **How** to review the Security Rules that determine whether the scanner's reads will succeed.

`panels.txt` should contain **only** Firebase databases that you own or have explicit authorization to use. If you obtained a URL from a public source without authorization, remove it.

Format:

```
# Comments allowed
https://your-project.firebaseio.com|AIzaSyYourWebApiKey
https://your-project-default-rtdb.firebasedatabase.app|
```

---

## 21. References

- Firebase Realtime Database REST API — https://firebase.google.com/docs/reference/rest/database
- Firebase REST API setup — https://firebase.google.com/docs/database/rest/start
- Realtime Database Security Rules — https://firebase.google.com/docs/database/security
- Security Rules reference — https://firebase.google.com/docs/reference/security/database
- Manage rules via REST — https://firebase.google.com/docs/database/rest/app-management
- Firebase App Check — https://firebase.google.com/docs/app-check
- OWASP Mobile Security Testing Guide — https://owasp.org/www-project-mobile-security-testing-guide/
- Google Cloud API key best practices — https://cloud.google.com/docs/authentication/api-keys

---

## 22. Responsible Use

**Intended audience:**

- Application developers
- Security engineers
- Authorized penetration testers
- Bug bounty researchers operating within program scope
- Defensive security teams
- Firebase project owners

**Not permitted:**

- Enumerating third-party Firebase databases from public code
- Collecting personal information from databases you do not own
- Bypassing authentication or Security Rules on systems outside your scope
- Modifying data without explicit authorization

**Core principle:** A Firebase configuration being visible in a client does not establish unauthorized access — **Security Rules** determine what is permitted. Test only what you own or are authorized to assess.

---

## License & Attribution

This document complements the [Jio-Gemini-Activation-Scanner](https://github.com/OshekharO/Jio-Gemini-Activation-Scanner) project and inherits its educational-use-only intent. See the main `README.md` for the project disclaimer.
