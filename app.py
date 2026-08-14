from datetime import datetime
import json
import os
import time
import urllib.parse
import urllib.request
from google import genai
from google.genai import types
import gspread
import streamlit as st

# ==========================================
# 1. 設定項目 (st.secrets から安全に取得)
# ==========================================
# Gemini API Key の取得
GEMINI_API_KEY = st.secrets.get(
    "GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", "")
)

# Chatwork 設定の取得
CHATWORK_API_TOKEN = st.secrets.get("CHATWORK_API_TOKEN", "")
CHATWORK_ROOM_ID = st.secrets.get("CHATWORK_ROOM_ID", "")

# スプレッドシート名とローカルフォールバック用の鍵ファイル名
SPREADSHEET_NAME = "音声アプリ"
LOCAL_CREDENTIALS_FILE = "sheet_key.json"

# 画面設定
st.set_page_config(
    page_title="音声台数表アプリ", page_icon="🎙️", layout="centered"
)
st.title("🎙️ 音声台数表 自動入力アプリ")
st.write(
    "スマホから直接声を吹き込んで、台数表の更新とChatwork報告を行います。"
)


# --- Google スプレッドシート接続処理 ---
def get_gspread_client():
    """Streamlit Secrets、またはローカルjsonファイルから認証してgspreadクライアントを返す"""
    if "gcp_service_account" in st.secrets:
        # Streamlit Cloud の Secrets から辞書形式で読み込み
        creds_dict = dict(st.secrets["gcp_service_account"])
        return gspread.service_account_from_dict(creds_dict)
    elif os.path.exists(LOCAL_CREDENTIALS_FILE):
        # ローカル環境の json ファイルから読み込み
        with open(LOCAL_CREDENTIALS_FILE, encoding="utf-8") as f:
            creds_data = json.load(f)
        return gspread.service_account_from_dict(creds_data)
    else:
        raise FileNotFoundError(
            "スプレッドシートの認証情報（Secrets または sheet_key.json）が見つかりません。"
        )


# --- Chatwork 送信用関数 ---
def send_chatwork_message(message):
    if not CHATWORK_API_TOKEN or not CHATWORK_ROOM_ID:
        st.warning(
            "⚠️ ChatworkのAPIトークンまたはルームIDが設定されていないため、通知をスキップしました。"
        )
        return False

    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages"
    headers = {
        "X-ChatWorkToken": CHATWORK_API_TOKEN,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = urllib.parse.urlencode({"body": message}).encode("utf-8")

    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as res:
            return True
    except Exception as e:
        st.error(f"Chatwork送信エラー: {e}")
        return False


# --- ワークシート取得・初期化関数 ---
def get_or_create_sheet(spreadsheet, sheet_name, headers):
    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows="100", cols="10")

    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(headers)
    return ws


# --- 音声処理メイン関数 ---
def process_audio(file_path):
    if not GEMINI_API_KEY:
        raise ValueError(
            "Gemini API Key が設定されていません。st.secrets を確認してください。"
        )

    client = genai.Client(api_key=GEMINI_API_KEY)

    # 1. スプレッドシート＆各タブの接続
    gc = get_gspread_client()
    spreadsheet = gc.open(SPREADSHEET_NAME)

    # タブ1: 音声ログ（履歴用）
    ws_log = get_or_create_sheet(
        spreadsheet, "音声ログ", ["登録日時", "場所・拠点", "台数", "音声全文"]
    )
    # タブ2: 台数一覧（マスター上書き用）
    ws_master = get_or_create_sheet(
        spreadsheet, "台数一覧", ["拠点名", "最新台数", "最終更新日時"]
    )

    # 2. Geminiへアップロード
    uploaded_file = client.files.upload(file=file_path)

    while uploaded_file.state.name == "PROCESSING":
        time.sleep(3)
        uploaded_file = client.files.get(name=uploaded_file.name)

    if uploaded_file.state.name == "FAILED":
        raise ValueError("音声の処理に失敗しました。")

    # 3. Geminiによる解析抽出
    prompt = """
    音声ファイルを聴き取り、「場所（拠点名）」と「台数（数値）」の組をすべて抽出してください。
    
    【出力フォーマット要件】
    以下のJSON形式で厳密に出力してください。台数は半角数字の数値（integer）のみにしてください。

    {
        "items": [
            {"location": "場所・拠点名", "count": 台数数字}
        ],
        "transcription": "音声全体の正確な文字起こし全文"
    }
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[uploaded_file, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    client.files.delete(name=uploaded_file.name)

    # 4. 解析データの反映（ログ追記 ＆ マスター上書き）
    result = json.loads(response.text)
    items = result.get("items", [])
    transcription = result.get("transcription", "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary_list = []

    if items:
        master_locations = ws_master.col_values(1)

        for item in items:
            loc = item.get("location", "").strip()
            cnt = item.get("count", 0)

            if not loc:
                continue

            # 音声ログヘ追加 (追記)
            ws_log.append_row([now, loc, cnt, transcription])

            # 台数一覧マスター表の該当セルを探して上書き
            row_index = None
            for idx, cell_value in enumerate(master_locations):
                if cell_value.strip() == loc:
                    row_index = idx + 1
                    break

            if row_index:
                ws_master.update_cell(row_index, 2, cnt)
                ws_master.update_cell(row_index, 3, now)
                summary_list.append(
                    f"・{loc}：{cnt}台 （マスター表を上書き更新）"
                )
            else:
                ws_master.append_row([loc, cnt, now])
                master_locations.append(loc)
                summary_list.append(
                    f"・{loc}：{cnt}台 （マスター表に新規追加）"
                )
    else:
        summary_list.append("・データ抽出なし")

    # 5. Chatwork通知
    details_str = "\n".join(summary_list)
    cw_message = f"""[info][title]📱 音声ログ記録＆マスター表更新完了[/title]日時: {now}

【更新内容】
{details_str}

[hr]
音声全文：{transcription}[/info]"""

    send_chatwork_message(cw_message)

    return now, summary_list, transcription


# --- UI画面レイアウト ---
st.subheader("① 音声を準備")
tab1, tab2 = st.tabs(["🎙️ スマホから直接録音", "📁 ファイルを選択"])

target_audio = None

with tab1:
    audio_recorded = st.audio_input(
        "タップして録音を開始（もう一度タップで停止）"
    )
    if audio_recorded is not None:
        target_audio = audio_recorded

with tab2:
    audio_uploaded = st.file_uploader(
        "録音済みファイルをアップロード",
        type=["m4a", "mp3", "wav", "aac"],
    )
    if audio_uploaded is not None:
        target_audio = audio_uploaded

st.divider()

st.subheader("② 処理を実行")
if target_audio is not None:
    if st.button("🚀 解析して登録・通知する", type="primary"):
        temp_path = "temp_input_audio.wav"
        with open(temp_path, "wb") as f:
            f.write(target_audio.getbuffer())

        with st.spinner(
            "録音データを解析中 ➔ スプレッドシート更新中 ➔ Chatwork送信中..."
        ):
            try:
                now, summary_list, transcription = process_audio(temp_path)

                st.success("✅ 処理が完了しました！")

                st.subheader("実行結果")
                st.write(f"**日時:** {now}")
                st.write("**更新データ:**")
                for item in summary_list:
                    st.write(f"- {item}")
                st.write(f"**全文文字起こし:** {transcription}")

            except Exception as e:
                st.error(f"エラーが発生しました: {e}")
else:
    st.info(
        "上の「直接録音」または「ファイル選択」で音声をセットしてください。"
    )