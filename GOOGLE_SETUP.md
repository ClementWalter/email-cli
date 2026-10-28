# Google setup for the Email extension

One time, about five minutes, free. Claude drives the user's Chrome with Claude
in Chrome; the user is signed in to Google in Chrome with the account to
connect. Pause and let the user act wherever Google asks for a password, a
2-step code, or to accept terms. Every link below opens in the Google Cloud
console; check the project picker at the top shows the right project before
each step.

One Google Cloud project and one OAuth client serve both the Email and the
Google Drive extensions. When the user also wants Drive, enable the Google
Drive APIs in step 2 too and reuse the same Client ID and Client secret: either
extension finds a client the other one saved. If such
a project already exists, select it and do only the missing steps.

## Steps

1. **Create a project.** Open https://console.cloud.google.com/projectcreate.
   Project name: `Claude Google access` (any name). Leave Organization / Location as they
   are. Click **Create**, wait for the notification, then select the project
   in the project picker at the top.
2. **Enable the APIs.** Open each of these and click **Enable**: Gmail
   https://console.cloud.google.com/apis/library/gmail.googleapis.com, and for
   the Google Drive extension too, Drive
   https://console.cloud.google.com/apis/library/drive.googleapis.com, Sheets
   https://console.cloud.google.com/apis/library/sheets.googleapis.com and Docs
   https://console.cloud.google.com/apis/library/docs.googleapis.com. Enabling
   all four now saves a second visit.
3. **Configure the consent screen.** Open
   https://console.cloud.google.com/auth/overview. If it says "Google Auth
   Platform not configured yet", click **Get started**:
   - App Information: App name `Claude Google access` (any), User support email: the
     user's address. **Next**.
   - Audience: **External**. **Next**. (A Google Workspace user connecting only
     addresses of their own organization may pick **Internal** instead: no
     test users, no 7-day expiry, no warning; skip steps 4 and 6.)
   - Contact Information: the user's address. **Next**.
   - Finish: tick the box agreeing to the Google API Services: User Data
     Policy, **Continue**, then **Create**.
4. **Add the user as a test user.** Open
   https://console.cloud.google.com/auth/audience. Under **Test users** click
   **Add users**, type every Gmail address to connect, **Save**.
5. **Create the OAuth client.** Open
   https://console.cloud.google.com/auth/clients and click **Create client**.
   Application type: **Desktop app**. Name: `Claude Google access`. Leave "Use this
   client for an AI-powered agent" unticked. **Create**. The "OAuth client
   created" dialog shows the Client ID and the Client secret only this once:
   click **Download JSON** (it lands in Downloads as `client_secret_….json`).
   If the dialog was closed, open the client and click **Add secret** to get a
   new one.
6. **Stop the weekly sign-out (recommended).** While the app's publishing
   status is **Testing**, Google ends the sign-in 7 days after consent. On
   https://console.cloud.google.com/auth/audience, under **Publishing status**,
   click **Publish app**, then **Confirm**: the status becomes **In
   production**. No verification is needed for an app only its owner uses
   (Google caps unverified apps at 100 users); the cost is a "Google hasn't
   verified this app" warning at each sign-in. Skipping this step works too:
   call `connect` again every week.
7. **Give the client to the extension**, one of:
   - Claude Desktop → Settings → Extensions → Email → Configure: paste the
     Client ID and the Client secret, **Save**.
   - Leave the downloaded JSON in Downloads: `connect` adopts the newest
     `client_secret_*.json` there.
   - Call `connect` with `client_file` set to the JSON's path.
8. **Connect.** Call `connect` (access `read` by default; `write` also lets
   Claude send, reply and manage drafts, each send still needing the user's
   confirmation). A Google page opens in the browser: choose the account. On
   "Google hasn't verified this app" click **Continue**, or **Advanced** then
   **Go to Claude Google access (unsafe)**: the app is the user's own. Tick every
   permission box, **Continue**. Then call `connect_status`. For another
   address, call `connect` with another `account` name (`work`, ...).

## Mac alternative without Google

Accounts already set up in the macOS Mail app (iCloud, Outlook, any IMAP, and
Gmail too) need no Google project: call `connect` with `mailapp` set to the
account's name in Mail (Mail → Settings → Accounts). macOS asks once to let
Claude control Mail. Search is slower than with the Gmail API.

## Troubleshooting

- "Access blocked: … has not completed the Google verification process"
  (`access_denied`): the address is not a test user (step 4) while the app is in
  Testing.
- "Gmail API has not been used in project …": step 2.
- `redirect_uri_mismatch` or `invalid_client`: the client is not of type Desktop
  app, or the secret was mistyped; redo step 5.
- If Google refuses the sign-in after step 6, click **Back to testing** on the
  Audience page and reconnect weekly.
