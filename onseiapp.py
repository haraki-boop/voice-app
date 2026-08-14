from datetime import datetime
import json
import os
import time
import urllib.parse
import urllib.request
import base64
from google import genai
from google.genai import types
import gspread
import streamlit as st

# ==========================================
# 設定項目 (st.secrets から安全に読み込み)
# ==========================================
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
SPREADSHEET_NAME = "音声アプリ"
LOCAL_CREDENTIALS_FILE = "sheet_key.json"
CHATWORK_API_TOKEN = st.secrets.get("CHATWORK_API_TOKEN", "")
CHATWORK_ROOM_ID = st.secrets.get("CHATWORK_ROOM_ID", "434281068")

# 画面設定
st.set_page_config(page_title="音声台数表", page_icon="🎙️", layout="centered")

# レイアウト調整
st.markdown(
    """
    <style>
        .block-container {
            padding-top: 2.5rem !important;
            padding-bottom: 2rem !important;
            padding-left: 1rem !important;
            padding-right: 1rem !important;
        }
        .stButton>button {
            width: 100% !important;
            height: 3.5rem !important;
            font-size: 1.2rem !important;
            font-weight: bold !important;
            border-radius: 12px !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🎙️ 音声台数表 自動入力")


# --- スプレッドシート接続用関数 ---
def get_gspread_client():
    if "gcp_service_account" in st.secrets:
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace(
                "\\n", "\n"
            )
        return gspread.service_account_from_dict(creds_dict)
    elif "GCP_JSON" in st.secrets:
        creds_dict = json.loads(st.secrets["GCP_JSON"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace(
                "\\n", "\n"
            )
        return gspread.service_account_from_dict(creds_dict)
    elif os.path.exists(LOCAL_CREDENTIALS_FILE):
        with open(LOCAL_CREDENTIALS_FILE, encoding="utf-8") as f:
            creds_data = json.load(f)
        return gspread.service_account_from_dict(creds_data)
    else:
        raise FileNotFoundError("スプレッドシートの認証情報が見つかりません。")


# --- Chatwork送信用関数 ---
def send_chatwork_message(message):
    if not CHATWORK_API_TOKEN:
        st.warning("Chatwork APIトークンが設定されていません。")
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
        raise ValueError("Gemini APIキーが設定されていません。")

    client = genai.Client(api_key=GEMINI_API_KEY)

    # 1. スプレッドシート＆各タブの接続
    gc = get_gspread_client()
    spreadsheet = gc.open(SPREADSHEET_NAME)

    ws_log = get_or_create_sheet(
        spreadsheet, "音声ログ", ["登録日時", "場所・拠点", "台数", "音声全文"]
    )
    ws_master_table = get_or_create_sheet(
        spreadsheet, "台数一覧", ["拠点名", "最新台数", "最終更新日時"]
    )
    ws_master_dict = get_or_create_sheet(
        spreadsheet, "拠点マスター", ["正式拠点名", "誤認しやすいキーワード"]
    )

    # 2. 拠点マスターから自動補正情報を取得
    master_rows = ws_master_dict.get_all_values()[1:]

    master_info = []
    for row in master_rows:
        if not row or not row[0].strip():
            continue
        official_name = row[0].strip()
        aliases = row[1].strip() if len(row) > 1 and row[1] else ""
        if aliases:
            master_info.append(
                f"・正式名称:「{official_name}」（誤認・言い間違い候補: {aliases}）"
            )
        else:
            master_info.append(f"・正式名称:「{official_name}」")

    locations_prompt_text = (
        "\n".join(master_info) if master_info else "（登録なし）"
    )

    # 3. Geminiアップロード
    uploaded_file = client.files.upload(file=file_path)

    while uploaded_file.state.name == "PROCESSING":
        time.sleep(3)
        uploaded_file = client.files.get(name=uploaded_file.name)

    if uploaded_file.state.name == "FAILED":
        raise ValueError("音声の処理に失敗しました。")

    # 4. Gemini解析（拠点マスター参照プロンプト）
    prompt = f"""
    音声ファイルを聴き取り、「場所（拠点名）」と「台数（数値）」の組をすべて抽出してください。

    【★最優先ルール：拠点マスターによる自動変換・補正】
    以下は社内の「正解の拠点マスターリスト」です：
    {locations_prompt_text}

    音声で話されている場所が、上記マスターリストの「正式名称」または「誤認・言い間違い候補」に該当する場合は、
    【必ず対応する正式名称】に自動補正・統一して出力してください。
    （例：「都筑」や「つなしま」と聴き取れても、マスターに「横浜綱島」があれば「横浜綱島」として出力する）

    ※マスターに全く該当しない明らかに新しい拠点の場合のみ、聴き取った名称をそのまま出力してください。

    【出力フォーマット要件】
    以下のJSON形式で厳密に出力してください。台数は半角数字の数値（integer）のみにしてください。

    {{
        "items": [
            {{"location": "場所・拠点名", "count": 台数数字}}
        ],
        "transcription": "音声全体の正確な文字起こし全文"
    }}
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[uploaded_file, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    client.files.delete(name=uploaded_file.name)

    # 5. データ解析＆スプレッドシート更新
    result = json.loads(response.text)
    items = result.get("items", [])
    transcription = result.get("transcription", "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary_list = []

    if items:
        master_locations = ws_master_table.col_values(1)

        for item in items:
            loc = item.get("location", "").strip()
            cnt = item.get("count", 0)

            if not loc:
                continue

            # 音声ログへ履歴追加
            ws_log.append_row([now, loc, cnt, transcription])

            # 台数一覧の該当行を検索して上書き
            row_index = None
            for idx, cell_value in enumerate(master_locations):
                if cell_value.strip() == loc:
                    row_index = idx + 1
                    break

            if row_index:
                ws_master_table.update_cell(row_index, 2, cnt)
                ws_master_table.update_cell(row_index, 3, now)
                summary_list.append(f"・{loc}：{cnt}台 （上書き更新）")
            else:
                ws_master_table.append_row([loc, cnt, now])
                master_locations.append(loc)
                summary_list.append(f"・{loc}：{cnt}台 （新規追加）")
    else:
        summary_list.append("・データ抽出なし")

    # 6. Chatwork通知
    details_str = "\n".join(summary_list)
    cw_message = f"""[info][title]📱 音声ログ記録＆マスター表更新完了[/title]日時: {now}

【更新内容】
{details_str}

[hr]
音声全文：{transcription}[/info]"""

    send_chatwork_message(cw_message)

    return now, summary_list, transcription


# --- UI画面レイアウト ---
tab1, tab2 = st.tabs(["🎙️ ボイスメモ録音", "📁 ファイル選択"])

target_audio_bytes = None

with tab1:
    # iPhoneボイスメモ風の単一円形ボタンUI
    iphone_recorder_html = """
    <div style="text-align: center; padding: 15px 0;">
        <div id="recordBtnOuter" onclick="toggleRecording()" style="
            width: 80px; height: 80px; border-radius: 50%; border: 4px solid #d1d1d6; 
            display: flex; align-items: center; justify-content: center; margin: 0 auto; 
            cursor: pointer; transition: all 0.2s ease; background-color: #ffffff;
            box-shadow: 0 4px 10px rgba(0,0,0,0.15);">
            <div id="recordBtnInner" style="
                width: 60px; height: 60px; border-radius: 50%; background-color: #ff3b30;
                transition: all 0.2s ease;"></div>
        </div>

        <div id="timerDisplay" style="
            margin-top: 15px; font-size: 22px; font-weight: bold; font-family: -apple-system, BlinkMacSystemFont, sans-serif; color: #1c1c1e;">
            00:00
        </div>
        <div id="statusLabel" style="
            margin-top: 5px; font-size: 14px; color: #8e8e93; font-weight: 500;">
            タップして録音開始
        </div>

        <audio id="audioPlayback" controls style="display:none; width: 100%; margin-top: 15px; border-radius: 8px;"></audio>
    </div>

    <script>
        let mediaRecorder;
        let audioChunks = [];
        let isRecording = false;
        let timerInterval;
        let seconds = 0;

        async function toggleRecording() {
            if (!isRecording) {
                // 録音開始
                try {
                    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    mediaRecorder = new MediaRecorder(stream);
                    audioChunks = [];

                    mediaRecorder.ondataavailable = event => {
                        audioChunks.push(event.data);
                    };

                    mediaRecorder.onstop = () => {
                        const audioBlob = new Blob(audioChunks, { type: 'audio/wav' });
                        const audioUrl = URL.createObjectURL(audioBlob);
                        const audioPlayback = document.getElementById('audioPlayback');
                        audioPlayback.src = audioUrl;
                        audioPlayback.style.display = 'block';

                        const reader = new FileReader();
                        reader.readAsDataURL(audioBlob);
                        reader.onloadend = () => {
                            const base64Audio = reader.result.split(',')[1];
                            window.parent.postMessage({
                                type: 'streamlit:setComponentValue',
                                value: base64Audio
                            }, '*');
                        };
                    };

                    mediaRecorder.start();
                    isRecording = true;

                    // UI変更（iPhoneボイスメモ風：四角ボタン・外枠赤点滅）
                    const outer = document.getElementById('recordBtnOuter');
                    const inner = document.getElementById('recordBtnInner');
                    outer.style.borderColor = '#ff3b30';
                    inner.style.borderRadius = '8px';
                    inner.style.width = '30px';
                    inner.style.height = '30px';

                    document.getElementById('statusLabel').innerText = '録音中... タップして停止';
                    document.getElementById('statusLabel').style.color = '#ff3b30';

                    // タイマー開始
                    seconds = 0;
                    document.getElementById('timerDisplay').innerText = '00:00';
                    timerInterval = setInterval(() => {
                        seconds++;
                        const m = String(Math.floor(seconds / 60)).padStart(2, '0');
                        const s = String(seconds % 60).padStart(2, '0');
                        document.getElementById('timerDisplay').innerText = `${m}:${s}`;
                    }, 1000);

                } catch (err) {
                    alert('マイクの使用許可が必要です: ' + err);
                }
            } else {
                // 録音停止
                if (mediaRecorder && mediaRecorder.state !== 'inactive') {
                    mediaRecorder.stop();
                    mediaRecorder.stream.getTracks().forEach(track => track.stop());
                }
                isRecording = false;
                clearInterval(timerInterval);

                // UIリセット
                const outer = document.getElementById('recordBtnOuter');
                const inner = document.getElementById('recordBtnInner');
                outer.style.borderColor = '#d1d1d6';
                inner.style.borderRadius = '50%';
                inner.style.width = '60px';
                inner.style.height = '60px';

                document.getElementById('statusLabel').innerText = '✅ 録音完了！';
                document.getElementById('statusLabel').style.color = '#34c759';
            }
        }
    </script>
    """

    recorded_base64 = st.components.v1.html(iphone_recorder_html, height=210)

    if "recorded_audio_bytes" not in st.session_state:
        st.session_state["recorded_audio_bytes"] = None

    if recorded_base64:
        st.session_state["recorded_audio_bytes"] = base64.b64decode(
            recorded_base64
        )

    if st.session_state["recorded_audio_bytes"] is not None:
        target_audio_bytes = st.session_state["recorded_audio_bytes"]

with tab2:
    audio_uploaded = st.file_uploader(
        "ファイルを選択",
        type=["m4a", "mp3", "wav", "aac"],
    )
    if audio_uploaded is not None:
        target_audio_bytes = audio_uploaded.getvalue()

st.markdown("---")

if target_audio_bytes is not None:
    if st.button("🚀 解析して登録・通知する", type="primary"):
        temp_path = "temp_input_audio.wav"
        with open(temp_path, "wb") as f:
            f.write(target_audio_bytes)

        with st.spinner("解析・スプレッドシート更新中..."):
            try:
                now, summary_list, transcription = process_audio(temp_path)

                st.success("✅ 処理が完了しました！")

                st.markdown("**【実行結果】**")
                st.write(f"・日時: {now}")
                for item in summary_list:
                    st.write(f"・{item}")
                st.caption(f"文字起こし: {transcription}")

            except Exception as e:
                st.error(f"エラーが発生しました: {e}")
else:
    st.info("💡 中央のボタンで録音するか、「ファイル選択」で音声をセットしてください。")