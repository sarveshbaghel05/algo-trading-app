import base64
import json
import requests
import streamlit as st

GITHUB_USER = "Sarveshbaghel05"
REPO_NAME = "algo-trading-app"  # यदि रिपॉजिटरी का नाम अलग है, तो यहाँ बदलें
FILE_PATH = "saved_trades.json"


def load_trades():
    token = st.secrets.get("GITHUB_TOKEN", "")
    if not token:
        return []
    url = f"https://api.github.com/repos/{GITHUB_USER}/{REPO_NAME}/contents/{FILE_PATH}"
    headers = {"Authorization": f"token {token}"}
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        content = base64.b64decode(res.json()["content"]).decode("utf-8")
        return json.loads(content)
    return []


def save_trades(trades_list):
    token = st.secrets.get("GITHUB_TOKEN", "")
    if not token:
        st.error("GITHUB_TOKEN Secrets में नहीं मिला!")
        return False
    url = f"https://api.github.com/repos/{GITHUB_USER}/{REPO_NAME}/contents/{FILE_PATH}"
    headers = {"Authorization": f"token {token}"}

    # पहले पुरानी फाइल का SHA चेक करें ताकि अपडेट हो सके
    res = requests.get(url, headers=headers)
    sha = res.json().get("sha") if res.status_code == 200 else None

    encoded_content = base64.b64encode(
        json.dumps(trades_list, indent=2).encode("utf-8")
    ).decode("utf-8")
    data = {
        "message": "Update saved trades from Streamlit app",
        "content": encoded_content,
    }
    if sha:
        data["sha"] = sha

    put_res = requests.put(url, headers=headers, json=data)
    return put_res.status_code in [200, 201]
