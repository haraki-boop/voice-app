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
# 設定項目 (st.secrets から安全に読み込み)
# ==========================================
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
SPREADSHEET_NAME = "音声アプリ"          # Googleスプレッドシートのファイル名
TEMPLATE_SHEET_NAME = "台数表（原本）"  # コピー元にするタブの名前
LOG_SHEET_NAME = "音声ログ"             # ログ保存用のタブの名前
LOCAL_CREDENTIALS_FILE = "sheet_key.json"
CHATWORK_API_TOKEN = st.secrets.get("CHATWORK_API_TOKEN", "")
CHATWORK_ROOM_ID = st.secrets.get("CHATWORK_ROOM_ID", "434281068")

st.set_page_config(page_title="音声台数表アプリ", page_icon="🎙️")
st.title("🎙️ 音声台数表 自動入力アプリ")


# --- スプレッドシート接続関数 ---
def get_gspread_client():
    if "gcp_service_account" in st.secrets:
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        return gspread.service_account_from_dict(creds_dict)
    elif "GCP_JSON" in st.secrets:
        creds_dict = json.loads(st.secrets["GCP_JSON"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        return gspread.service_account_from_dict(creds_dict)
    elif os.path.exists(LOCAL_CREDENTIALS_FILE):
        with open(LOCAL_CREDENTIALS_FILE, encoding="utf-8") as f:
            creds_data = json.load(f)
        return gspread.service_account_from_dict(creds_data)
    else:
        raise FileNotFoundError("スプレッドシートの認証情報が見つかりません。")


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


# --- Chatwork送信関数 ---
def send_chatwork_message(message):
    if not CHATWORK_API_TOKEN:
        return False
    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages"
    headers = {
        "X-ChatWorkToken": CHATWORK_API_TOKEN,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = urllib.parse.urlencode({"body": message}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as res:
            return True
    except Exception:
        return False


# --- 音声処理メイン関数 ---
def process_audio(file_path):
    if not GEMINI_API_KEY:
        raise ValueError("Gemini APIキーが設定されていません。")

    client = genai.Client(api_key=GEMINI_API_KEY)

    gc = get_gspread_client()
    spreadsheet = gc.open(SPREADSHEET_NAME)

    # 「音声ログ」タブの取得（なければ自動作成）
    ws_log = get_or_create_sheet(
        spreadsheet, LOG_SHEET_NAME, ["登録日時", "対象シート", "店舗名", "台数", "音声全文"]
    )

    # 1. データ受信（実行）時点の「日付」と「曜日」を自動生成
    now_dt = datetime.now()
    weekdays_jp = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]
    date_str = f"{now_dt.month}月{now_dt.day}日"
    day_of_week = weekdays_jp[now_dt.weekday()]

    # 2. Geminiアップロード
    uploaded_file = client.files.upload(file=file_path)
    while uploaded_file.state.name == "PROCESSING":
        time.sleep(2)
        uploaded_file = client.files.get(name=uploaded_file.name)

    # 3. Gemini解析（503混雑エラー対策のリトライ処理付き）
    prompt = """
    音声ファイルを聴き取り、以下の【便情報】と【店舗・台数情報】を抽出してください。

    【便情報】
    ・bin_str: 便情報（例: "(2便)" や "(1便)"）。音声で言っていない場合は "(2便)" を標準とする。

    【店舗・台数情報】
    ・「店舗名」と「台数（数値）」の組を抽出。
    ・店舗名補正ルール:
        - 「文庫」「金沢」 ➔ 「金沢文庫」
        - 「戸塚」 ➔ 「戸塚」
        - 「長津田」 ➔ 「長津田」
        - 「綱島」「横浜綱島」「つなしま」 ➔ 「横浜綱島」

    【出力JSON形式】
    {
        "bin_str": "(2便)",
        "items": [
            {"location": "正式店舗名", "count": 台数数字}
        ],
        "transcription": "音声全文"
    }
    """

    response = None
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            break
        except Exception as e:
            if ("503" in str(e) or "UNAVAILABLE" in str(e)) and attempt < max_retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            client.files.delete(name=uploaded_file.name)
            raise e

    client.files.delete(name=uploaded_file.name)

    result = json.loads(response.text)
    bin_str = result.get("bin_str", "(2便)")
    items = result.get("items", [])
    transcription = result.get("transcription", "")
    now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    # 4. シートの複製・取得処理
    clean_bin = bin_str.replace("(", "").replace(")", "").replace("便", "")
    new_sheet_name = f"{date_str}_{clean_bin}便" if clean_bin != "便指定なし" else date_str

    try:
        ws = spreadsheet.worksheet(new_sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        try:
            template_ws = spreadsheet.worksheet(TEMPLATE_SHEET_NAME)
        except Exception:
            template_ws = spreadsheet.sheet1
        
        ws = template_ws.duplicate(new_sheet_name=new_sheet_name)

    # 5. 受信時の「日付・曜日」および音声から抽出した「便」の書き込み
    ws.update_cell(1, 2, date_str)      # B1セル：受領日の日付
    ws.update_cell(1, 3, day_of_week)   # C1セル：受領日の曜日
    ws.update_cell(1, 4, bin_str)       # D1セル：便

    # 6. 店舗台数の書き込み＆「音声ログ」タブへ履歴追加
    a_column_values = ws.col_values(1)
    summary_list = [f"【シート作成/更新】{new_sheet_name}"]
    summary_list.append(f"・受領日: {date_str} ({day_of_week}) {bin_str}")

    if items:
        for item in items:
            loc = item.get("location", "").strip()
            cnt = item.get("count", 0)

            if not loc:
                continue

            # 「音声ログ」タブへ無条件で1件ずつ追加保存
            ws_log.append_row([now_time_str, new_sheet_name, loc, cnt, transcription])

            row_index = None
            for idx, cell_value in enumerate(a_column_values):
                if cell_value.strip() == loc:
                    row_index = idx + 1
                    break

            if row_index:
                ws.update_cell(row_index, 6, cnt)  # F列（一括）に台数を書き込み
                summary_list.append(f"・{loc}：{cnt}台")
            else:
                summary_list.append(f"・{loc}：店舗名がシートに見つかりません")
    else:
        # データが抽出できなかった場合も全文だけログに残す
        ws_log.append_row([now_time_str, new_sheet_name, "なし", 0, transcription])
        summary_list.append("・台数データ抽出なし")

    # 7. Chatworkへ通知
    details_str = "\n".join(summary_list)
    cw_message = f"""[info][title]📱 自動シート作成＆台数入力完了[/title]処理日時: {now_time_str}

{details_str}

[hr]
音声全文：{transcription}[/info]"""

    send_chatwork_message(cw_message)

    return now_time_str, summary_list, transcription


# --- UI画面 ---
st.subheader("① 音声を準備")
tab1, tab2 = st.tabs(["🎙️ スマホから直接録音", "📁 ファイルを選択"])

target_audio = None

with tab1:
    audio_recorded = st.audio_input("タップして録音を開始（もう一度タップで停止）")
    if audio_recorded is not None:
        st.success("✅ 音声データがセットされました！")
        target_audio = audio_recorded

with tab2:
    audio_uploaded = st.file_uploader("録音済みファイルをアップロード", type=["m4a", "mp3", "wav", "aac"])
    if audio_uploaded is not None:
        target_audio = audio_uploaded

st.divider()

st.subheader("② 処理を実行")
if target_audio is not None:
    if st.button("🚀 自動コピーしてデータ入力する", type="primary"):
        temp_path = "temp_input_audio.wav"
        with open(temp_path, "wb") as f:
            f.write(target_audio.getbuffer())

        with st.spinner("原本コピー・本日日付適用・台数更新・ログ記録中..."):
            try:
                now_time_str, summary_list, transcription = process_audio(temp_path)
                st.success("✅ 処理が完了しました！")
                st.subheader("実行結果")
                st.write(f"**日時:** {now_time_str}")
                for item in summary_list:
                    st.write(item)
                st.write(f"**全文文字起こし:** {transcription}")
            except Exception as e:
                st.error(f"エラーが発生しました: {e}")