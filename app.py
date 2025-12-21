import sqlite3
import uuid
import math
import re
import unicodedata
import os # OSの環境変数を読むため
from dotenv import load_dotenv # .envファイルを読み込むため
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, session, make_response, jsonify
from markupsafe import Markup
import time


# ★管理者用の合言葉（好きな文字に変更してください）
# .envファイルを読み込む
load_dotenv()

app = Flask(__name__)

# ★重要：セッションを暗号化するための鍵をセット
# .envから読み込む（なければデフォルト値を使う）
app.secret_key = os.getenv('SECRET_KEY', 'default_secret_key')

# 管理者パスワードも.envから読み込む
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'secret')

# --- データベース初期化 ---
def init_db():
    conn = sqlite3.connect('sns.db')
    c = conn.cursor()
    # id, 名前, 本文, ユーザーID, 日時, いいね数
    c.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY, 
            name TEXT, 
            content TEXT, 
            user_id TEXT, 
            created_at TEXT, 
            likes INTEGER DEFAULT 0,
            converted_content TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- カスタムフィルター（>>数字 をリンクに変換） ---
REPLY_PATTERN = re.compile(r'>>(\d+)')

@app.template_filter('linkify_reply')
def linkify_reply(content):
    # 修正前: href="#post-\1"
    # 修正後: href="/jump/\1"  (一度サーバーの /jump/数字 に飛ばす)
    linked_content = REPLY_PATTERN.sub(r'<a href="/jump/\1" class="text-decoration-none">>>\1</a>', content)
    return Markup(linked_content)

@app.route('/jump/<int:post_id>')
def jump(post_id):
    conn = sqlite3.connect('sns.db')
    c = conn.cursor()

    # 1. その投稿が存在するか確認
    c.execute("SELECT id FROM posts WHERE id = ?", (post_id,))
    result = c.fetchone()
    
    if not result:
        conn.close()
        # 投稿がない場合はトップページへ戻す（エラー表示などは今回は省略）
        return redirect('/')

    # 2. その投稿が「新しい順」で何番目にあるかを数える
    # (自分よりIDが大きい投稿の数 + 1) が、自分の順位です
    c.execute("SELECT COUNT(*) FROM posts WHERE id >= ?", (post_id,))
    rank = c.fetchone()[0]
    conn.close()

    # 3. ページ番号を計算
    per_page = 10  # 1ページあたりの件数（index関数と同じにする！）
    target_page = math.ceil(rank / per_page)

    # 4. そのページへリダイレクト（#post-数字 をつけて、特定の位置までスクロールさせる）
    return redirect(f'/?page={target_page}#post-{post_id}')


# --- メイン機能 ---
@app.route('/', methods=['GET', 'POST'])
def index():
    # 1. Cookie情報の取得
    user_id = request.cookies.get('user_id')
    if not user_id:
        user_id = str(uuid.uuid4()) # IDがなければ発行
    
    saved_name = request.cookies.get('saved_name', "")

    is_admin = session.get('is_admin', False)

    conn = sqlite3.connect('sns.db')
    c = conn.cursor()

    # --- POST（投稿時）の処理 ---
    if request.method == 'POST':
        name = request.form['name'] 
        content = request.form['content']
        created_at = datetime.now(ZoneInfo("Asia/Tokyo")).strftime('%Y-%m-%d %H:%M')
        converted = WabunMorseConverter().convert(content)

        
        if not name: name = "名無しさん"

        c.execute("INSERT INTO posts (name, content, user_id, created_at, converted_content) VALUES (?, ?, ?, ?, ?)", 
                  (name, content, user_id, created_at, converted))
        conn.commit()
        conn.close()
        
        # Cookie保存（IDと名前を30日間記憶）
        resp = make_response(redirect('/'))
        resp.set_cookie('user_id', user_id, max_age=60*60*24*30)
        resp.set_cookie('saved_name', name, max_age=60*60*24*30)
        return resp

    # --- GET（表示時）の処理 ---
    else:
        conn = sqlite3.connect('sns.db')
        c = conn.cursor()

        # ★追加1：固定投稿 (ID:1) を個別に取得
        c.execute("SELECT * FROM posts WHERE id = 1")
        fixed_post = c.fetchone()

        # A. ページネーション計算
        page = request.args.get('page', 1, type=int)
        per_page = 10
        offset = (page - 1) * per_page
        
        # B. 並び替え
        sort_by = request.args.get('sort')
        if sort_by == 'likes':
            order_sql = "ORDER BY likes DESC"
        else:
            order_sql = "ORDER BY id DESC"

        # ★変更2：通常のリストからは ID:1 を除外 (WHERE id != 1)
        # これにより、リストの中にID:1が重複して出るのを防ぎます
        c.execute(f"SELECT * FROM posts WHERE id != 1 {order_sql} LIMIT ? OFFSET ?", (per_page, offset))
        posts = c.fetchall()

        # 全件数カウント（ページネーション用）も ID:1 以外で数える
        c.execute("SELECT COUNT(*) FROM posts WHERE id != 1")
        total_posts = c.fetchone()[0]
        total_pages = math.ceil(total_posts / per_page)
        
        conn.close()
        
        return render_template('index.html', 
                               posts=posts, 
                               fixed_post=fixed_post, # ★固定投稿を渡す
                               current_user_id=user_id, 
                               saved_name=saved_name,
                               is_admin=is_admin,
                               page=page,
                               total_pages=total_pages,
                               sort_by=sort_by)

# ★追加3：固定投稿（ID:1）の編集・作成ルート
@app.route('/admin/edit_fixed', methods=['GET', 'POST'])
def edit_fixed():
    # 管理者じゃなければトップへ弾く
    if not session.get('is_admin'):
        return redirect('/')

    conn = sqlite3.connect('sns.db')
    c = conn.cursor()

    if request.method == 'POST':
        content = request.form['content']
        # 名前は「管理者」などで固定、あるいはフォームで変更可能にしてもOK
        name = "管理者"
        created_at = datetime.now(ZoneInfo("Asia/Tokyo")).strftime('%Y-%m-%d %H:%M')
        
        # ID:1 があるか確認
        c.execute("SELECT id FROM posts WHERE id = 1")
        if c.fetchone():
            # あれば更新 (UPDATE)
            c.execute("UPDATE posts SET name=?, content=?, created_at=? WHERE id=1", (name, content, created_at))
        else:
            # なければ ID=1 を指定して強制作成 (INSERT)
            # ユーザーIDは管理者として 'admin' などを入れておく
            c.execute("INSERT INTO posts (id, name, content, user_id, created_at) VALUES (1, ?, ?, 'admin', ?)", 
                      (name, content, created_at))
        
        conn.commit()
        conn.close()
        return redirect('/')

    else: # GET（編集画面の表示）
        c.execute("SELECT content FROM posts WHERE id = 1")
        result = c.fetchone()
        current_content = result[0] if result else "" # 今の内容を取得
        conn.close()
        
        # 編集用の簡易HTMLを返す（別ファイルを作らずここで書いてしまいます）
        return f'''
        <!DOCTYPE html>
        <html lang="ja">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
            <title>固定投稿の編集</title>
        </head>
        <body class="bg-light container mt-5" style="max-width: 600px;">
            <h2>固定投稿 (ID:1) の編集</h2>
            <form method="post">
                <div class="mb-3">
                    <label class="form-label">内容 (HTMLタグ使用可・文字制限なし)</label>
                    <textarea name="content" class="form-control" rows="10">{current_content}</textarea>
                </div>
                <button type="submit" class="btn btn-primary">保存して戻る</button>
                <a href="/" class="btn btn-secondary">キャンセル</a>
            </form>
        </body>
        </html>
        '''

# --- 削除機能 ---
@app.route('/delete/<int:post_id>', methods=['POST'])
def delete(post_id):
    current_user_id = request.cookies.get('user_id')
    is_admin = session.get('is_admin', False)

    conn = sqlite3.connect('sns.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM posts WHERE id = ?", (post_id,))
    result = c.fetchone()
    
    if result:
        owner_id = result[0]
        # 本人 または 管理者 なら削除実行
        if current_user_id == owner_id or is_admin:
            c.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            conn.commit()
    
    conn.close()
    return redirect('/')

# --- いいね機能 ---
@app.route('/like/<int:post_id>', methods=['POST'])
def like(post_id):
    # 1. セッションを使って「最後にいいねした時間」をチェック
    last_liked_time = session.get('last_liked_time', 0)
    current_time = time.time()
    
    # 前回から0.5秒未満なら、処理をスキップして今の数だけ返す（DB更新しない！）
    if current_time - last_liked_time < 0.5:
        conn = sqlite3.connect('sns.db')
        c = conn.cursor()
        c.execute("SELECT likes FROM posts WHERE id = ?", (post_id,))
        likes = c.fetchone()[0]
        conn.close()
        return jsonify({'likes': likes}) # 何もせず今の数を返す

    # 2. 0.5秒以上経っていれば、DB更新処理へ進む
    session['last_liked_time'] = current_time # 時間を更新

    conn = sqlite3.connect('sns.db')
    c = conn.cursor()
    c.execute("UPDATE posts SET likes = likes + 1 WHERE id = ?", (post_id,))
    conn.commit()
    
    c.execute("SELECT likes FROM posts WHERE id = ?", (post_id,))
    new_likes_count = c.fetchone()[0]
    conn.close()
    
    return jsonify({'likes': new_likes_count})

# --- 管理者ログイン ---
@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    msg = ""
    if request.method == 'POST':
        # .envから読み込んだパスワードと比較
        if request.form['password'] == ADMIN_PASSWORD:
            # ★変更点3：sessionに記録する
            # これだけで、暗号化されたCookieが自動でブラウザに送られます
            session['is_admin'] = True
            return redirect('/')
        else:
            msg = "パスワードが違います"
    
    return f'''
    <div style="text-align:center; margin-top:50px;">
        <h2>管理者ログイン</h2>
        <p style="color:red">{msg}</p>
        <form method="post">
            <input type="password" name="password" placeholder="合言葉">
            <button type="submit">ログイン</button>
        </form>
    </div>
    '''

# --- 管理者ログアウト ---
@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect('/')

# ★ここを追加：使い方ページのルート
@app.route('/about')
def about():
    # about.html を表示するだけ。特別な処理は不要。
    return render_template('about.html')

SMALL_KANA_MAP = str.maketrans({
    'ぁ': 'あ', 'ぃ': 'い', 'ぅ': 'う', 'ぇ': 'え', 'ぉ': 'お',
    'っ': 'つ', 'ゃ': 'や', 'ゅ': 'ゆ', 'ょ': 'よ', 'ゎ': 'わ',
    'ァ': 'ア', 'ィ': 'イ', 'ゥ': 'ウ', 'ェ': 'エ', 'ォ': 'オ',
    'ッ': 'ツ', 'ャ': 'ヤ', 'ュ': 'ユ', 'ョ': 'ヨ', 'ヮ': 'ワ',
    'ヵ': 'カ', 'ヶ': 'ケ'
})

class WabunMorseConverter:
    def __init__(self):
        # 基本的な和文モールス信号表
        self.morse_map = {
            'イ': '・－', 'ロ': '・－・－', 'ハ': '－・・・', 'ニ': '－・－・',
            'ホ': '－・・', 'ヘ': '・', 'ト': '・・－・・', 'チ': '・・－・',
            'リ': '－－・', 'ヌ': '・・・・', 'ル': '－・－－・', 'ヲ': '・－－－',
            'ワ': '－・－', 'カ': '・－・・', 'ヨ': '－－', 'タ': '－・',
            'レ': '－－－', 'ソ': '－－－・', 'ツ': '・－－・', 'ネ': '－－・－',
            'ナ': '・－・', 'ラ': '・・・', 'ム': '－', 'ウ': '・・－',
            'ヰ': '・－・・－', 'ノ': '・・－－', 'オ': '・－・・・', 'ク': '・・・－',
            'ヤ': '・－－', 'マ': '－・・－', 'ケ': '－・－－', 'フ': '－－・・',
            'コ': '－－－－', 'エ': '－・－－－', 'テ': '・－・－－', 'ア': '－－・－－',
            'サ': '－・－・－', 'キ': '－・－・・', 'ユ': '－・・－－', 'メ': '－・・・－',
            'ミ': '・・－・－', 'シ': '－－・－・', 'ヱ': '・－－・・', 'ヒ': '－－・・－',
            'モ': '－・・－・', 'セ': '・－－－・', 'ス': '－－－・－', 'ン': '・－・－・',
            # 濁点・半濁点
            '゛': '・・', '゜': '・・－・・',
            # 数字
            '1': '・－－－－', '2': '・・－－－', '3': '・・・－－', '4': '・・・・－', '5': '・・・・・',
            '6': '－・・・・', '7': '－－・・・', '8': '－－－・・', '9': '－－－－・', '0': '－－－－－',
            # 記号（一部）
            '、': '・－・－・－', '。': '・－・－・・', '（': '－・－－・－', '）': '・－・・－・',
            'ー': '・－－・－',  # 長音
        }

        
        
        # 結合文字（NFD正規化後の濁点・半濁点）のマッピングを追加
        # \u3099 は濁点(combining voiced sound mark)
        # \u309A は半濁点(combining semi-voiced sound mark)
        self.morse_map['\u3099'] = self.morse_map['゛']
        self.morse_map['\u309A'] = self.morse_map['゜']

    def convert(self, text):
    # アンカー部分 (>>数字) でテキストを分割する
    # () で囲むことで、区切り文字である >>数字 自体もリストに残ります
        parts = re.split(r'(>>\d+)', text)
    
        final_result = []

        for part in parts:
        # 1. アンカーの場合：そのままリストに追加
            if re.match(r'>>\d+', part):
                final_result.append(part)
                continue
        
        # 2. 空文字の場合（splitの結果生じることがある）：スキップ
            if not part:
                continue

        # 3. 通常テキストの場合：既存のロジックで変換
        # --- ここから元のロジック ---
            normalized_text = unicodedata.normalize('NFD', part)
            part_morse_code = []
        
            for char in normalized_text:

                char = char.translate(SMALL_KANA_MAP)


            # カタカナ化（ひらがなの場合）
                if 'ぁ' <= char <= 'ゖ':
                    char = chr(ord(char) + 0x60)
            
            # 辞書から取得
                code = self.morse_map.get(char)
            
                if code:
                    code = code.replace('・', 'お゛っ').replace('－', 'ほ')
                    part_morse_code.append(code+'♡')
                elif char.isspace():
                # 空白の処理
                    part_morse_code.append('　') 
        
        # 変換できたものがあれば結合して追加
            if part_morse_code:
                final_result.append(''.join(part_morse_code))
        # --- ここまで元のロジック ---

    # 全体をスペースでつないで返す
        return ''.join(final_result)

if __name__ == '__main__':
    app.run(debug=True)