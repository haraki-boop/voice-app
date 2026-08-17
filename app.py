from datetime import datetime, timedelta, timezone
import json
import os
import re
import time
import urllib.parse
import urllib.request
from google import genai
from google.genai import types
import gspread
import streamlit as st

# ==========================================
# 設定項目
# ==========================================
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
SPREADSHEET_NAME = "音声アプリ"          # スプレッドシート名
TEMPLATE_SHEET_NAME = "集計表（原本）"  # 原本タブ名
LOG_SHEET_NAME = "音声ログ"
LOCAL_CREDENTIALS_FILE = "sheet_key.json"
CHATWORK_API_TOKEN = st.secrets.get("CHATWORK_API_TOKEN", "")
CHATWORK_ROOM_ID = st.secrets.get("CHATWORK_ROOM_ID", "434281068")

# 日本標準時 (JST = UTC+9)
JST = timezone(timedelta(hours=9))

# カテゴリと列（C=3, D=4, E=5, F=6）のマッピング
CATEGORY_COL_MAP = {
    "水産（サイロ）": 3,
    "水産（構内）": 4,
    "畜産": 5,
    "おにぎり": 6
}

st.set_page_config(page_title="音声カゴ車数 自動入力", page_icon="🎙️")
st.title("🎙️ 音声カゴ車数 自動入力アプリ")

# --- スプレッドシート接続 ---
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

def get_or_create_sheet(spreadsheet, sheet_name, headers):
    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows="100", cols="10")
    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(headers)
    return ws

def send_chatwork_message(message):
    if not CHATWORK_API_TOKEN: return False
    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages"
    headers = {"X-ChatWorkToken": CHATWORK_API_TOKEN, "Content-Type": "application/x-www-form-urlencoded"}
    data = urllib.parse.urlencode({"body": message}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req): return True
    except Exception: return False

# --- 音声処理メイン関数 ---
def process_audio(file_path, selected_category):
    client = genai.Client(api_key=GEMINI_API_KEY)
    gc = get_gspread_client()
    spreadsheet = gc.open(SPREADSHEET_NAME)

    ws_log = get_or_create_sheet(spreadsheet, LOG_SHEET_NAME, ["登録日時", "対象シート", "カテゴリ", "店舗名", "台数", "音声全文"])

    now_dt = datetime.now(JST)
    weekdays_jp = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = f"{now_dt.month}月{now_dt.day}日"          # 例: 8月14日
    day_of_week = f"（{weekdays_jp[now_dt.weekday()]}）" # 例: （金）
    time_str = now_dt.strftime("%H:%M")                  # 例: 14:30

    uploaded_file = client.files.upload(file=file_path)
    while uploaded_file.state.name == "PROCESSING":
        time.sleep(2)
        uploaded_file = client.files.get(name=uploaded_file.name)

    # 抽出プロンプト（大→台への変換指示を追加）
    prompt = f"""
    音声ファイルを聴き取り、「店舗名」と「台数（数値）」の組を抽出してください。

    【最重要ルール】
    数字の直後にある「大」「だい」「ダイ」という発音は、必ず「台」に変換して出力してください。
    （例：「5大」「5だい」➔「5」）※台数の項目は数値のみ(integer)にしてください。

    【店舗名補正ルール】
    ・「大高」「三好」「守山」「かかみが原」「ながくて」など、愛知・岐阜周辺の店舗名が入ります。誤認識しやすい地名に注意してください。

    【出力JSON形式】
    {{
        "items": [
            {{"location": "店舗名", "count": 台数数字}}
        ],
        "transcription": "音声全文"
    }}
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
    items = result.get("items", [])
    raw_transcription = result.get("transcription", "")
    
    # 全文テキストに対しても強制的に「大/だい」を「台」に正規表現で置換
    transcription = re.sub(r'(\d+)\s*[大だダ][いイ]?', r'\1台', raw_transcription)
    now_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    # シート名の決定（当日日付）
    new_sheet_name = date_str

    try:
        ws = spreadsheet.worksheet(new_sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        try:
            template_ws = spreadsheet.worksheet(TEMPLATE_SHEET_NAME)
        except Exception:
            template_ws = spreadsheet.sheet1
        ws = template_ws.duplicate(new_sheet_name=new_sheet_name)

    # 日付・曜日・終了時間の更新
    ws.update_acell('D4', date_str)     # D4: 8月14日
    ws.update_acell('E4', day_of_week)  # E4: （金）
    ws.update_acell('F3', time_str)     # F3: 終了時間 (14:30)

    # 対象カテゴリの列番号を取得
    target_col_idx = CATEGORY_COL_MAP[selected_category]

    # B列（店舗名）の取得
    b_column_values = ws.col_values(2)
    summary_list = [f"【シート更新】{new_sheet_name}", f"【カテゴリ】{selected_category}", f"【処理時刻】{time_str}"]

    if items:
        # 複数セルをまとめて更新するためのリスト
        cell_updates = []
        for item in items:
            loc = item.get("location", "").strip()
            cnt = item.get("count", 0)

            if not loc: continue

            # ログ書き込み
            ws_log.append_row([now_time_str, new_sheet_name, selected_category, loc, cnt, transcription])

            row_index = None
            for idx, cell_value in enumerate(b_column_values):
                # 完全一致または含まれているかで判定
                if loc in cell_value.strip() or cell_value.strip() in loc:
                    row_index = idx + 1
                    break

            if row_index:
                # 対象カテゴリの列に台数をセット
                cell_updates.append({'range': f'{gspread.utils.rowcol_to_a1(row_index, target_col_idx)}', 'values': [[cnt]]})
                summary_list.append(f"・{loc}：{cnt}台")
            else:
                summary_list.append(f"・{loc}：※店舗が見つかりません")
        
        # 値の一括更新
        if cell_updates:
            ws.batch_update(cell_updates, value_input_option='USER_ENTERED')

    else:
        ws_log.append_row([now_time_str, new_sheet_name, selected_category, "なし", 0, transcription])
        summary_list.append("・データ抽出なし")

    # --- 合計・総合計の自動計算関数セット ---
    # G列（合計）に =SUM(C行:F行) を7行目〜32行目までセット
    g_col_formulas = [[f'=SUM(C{r}:F{r})'] for r in range(7, 33)]
    ws.update(range_name='G7:G32', values=g_col_formulas, value_input_option='USER_ENTERED')

    # 33行目（合計）と34行目（総合計）の式をセット
    bottom_formulas = [
        ['=SUM(C7:C32)', '=SUM(D7:D32)', '=SUM(E7:E32)', '=SUM(F7:F32)', '=SUM(G7:G32)'], # 33行目
        ['=SUM(C33:C33)', '=SUM(D33:D33)', '=SUM(E33:E33)', '=SUM(F33:F33)', '=SUM(G33:G33)'] # 34行目
    ]
    ws.update(range_name='C33:G34', values=bottom_formulas, value_input_option='USER_ENTERED')

    # Chatwork通知
    details_str = "\n".join(summary_list)
    cw_message = f"""[info][title]📱 {selected_category} カゴ車数入力完了[/title]日時: {now_time_str}

{details_str}

[hr]
音声全文：{transcription}[/info]"""

    send_chatwork_message(cw_message)

    return now_time_str, summary_list, transcription


# --- UI画面 ---
st.subheader("① 登録するカテゴリを選択")
# 録音前にカテゴリを選べるラジオボタン
selected_category = st.radio(
    "どの項目に数値を入力しますか？",
    ["水産（サイロ）", "水産（構内）", "畜産", "おにぎり"],
    horizontal=True
)

st.divider()

st.subheader("② 音声を準備")
tab1, tab2 = st.tabs(["🎙️ スマホから直接録音", "📁 ファイルを選択"])

target_audio = None

with tab1:
    audio_recorded = st.audio_input("タップして録音を開始（もう一度タップで停止）")
    if audio_recorded is not None:
        st.success(f"✅ {selected_category} の音声データがセットされました！")
        target_audio = audio_recorded

with tab2:
    audio_uploaded = st.file_uploader("録音済みファイルをアップロード", type=["m4a", "mp3", "wav", "aac"])
    if audio_uploaded is not None:
        target_audio = audio_uploaded

st.divider()

st.subheader("③ 処理を実行")
if target_audio is not None:
    if st.button("🚀 解析してデータを更新する", type="primary"):
        temp_path = "temp_input_audio.wav"
        with open(temp_path, "wb") as f:
            f.write(target_audio.getbuffer())

        with st.spinner(f"【{selected_category}】のデータを解析・更新中..."):
            try:
                now_time_str, summary_list, transcription = process_audio(temp_path, selected_category)
                st.success("✅ 処理が完了しました！")
                st.subheader("実行結果")
                for item in summary_list:
                    st.write(item)
                st.write(f"**全文文字起こし:** {transcription}")
            except Exception as e:
                st.error(f"エラーが発生しました: {e}")
else:
    st.info("上のマイクボタンを押して音声をセットしてください。")