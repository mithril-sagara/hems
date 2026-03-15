# 軽量なPythonイメージを使用
FROM python:3.10-slim

# 作業ディレクトリの設定
WORKDIR /app

# 必要なパッケージをインストール
# sqlite3の動作に必要なライブラリ等を含める
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 依存関係ファイルのコピーとインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ソースコードのコピー
COPY hems.py .

# Flaskのポート番号
EXPOSE 8000

# 起動コマンド
CMD ["python", "hems.py"]
