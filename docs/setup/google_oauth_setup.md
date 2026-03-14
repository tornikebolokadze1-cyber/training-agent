# Google Cloud OAuth Setup (Drive + Docs API)

## Step 1: Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project: `Training Agent`
3. Select the project

## Step 2: Enable APIs

1. Go to **APIs & Services** → **Library**
2. Search and enable:
   - **Google Drive API**
   - **Google Docs API**

## Step 3: Create OAuth Credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **OAuth client ID**
3. If prompted, configure the **OAuth consent screen**:
   - User type: **External** (or Internal if using Workspace)
   - App name: `Training Agent`
   - Add your email as a test user
4. Application type: **Desktop app**
5. Name: `Training Agent Desktop`
6. Click **Create**
7. **Download JSON** → save as `credentials.json` in the project root

## Step 4: First-Time Authorization

Run the Google Drive manager to trigger the OAuth flow:
```bash
cd "/Users/tornikebolokadze/Desktop/Training Agent"
python -m tools.gdrive_manager
```

This will:
1. Open a browser window for Google sign-in
2. Ask permission to access Drive and Docs
3. Save the refresh token to `token.json`

## Step 5: Get Drive Folder IDs

1. Open Google Drive in your browser
2. Navigate to "AI კურსი (მარტის ჯგუფი #1. 2026)"
3. The folder ID is in the URL: `drive.google.com/drive/folders/{FOLDER_ID}`
4. Copy the ID → paste into `.env` as `DRIVE_GROUP1_FOLDER_ID`
5. Repeat for Group #2 folder → `DRIVE_GROUP2_FOLDER_ID`

## Security Notes

- `credentials.json` and `token.json` are gitignored
- Token auto-refreshes — no manual intervention needed
- If the token expires completely, re-run the auth flow
