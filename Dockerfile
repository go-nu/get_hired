# 로컬에서 띄워 보기 위한 이미지. 개발 서버(runserver)로 실행한다.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# DB 표를 맞춘 뒤 서버를 켠다. CSS는 빌드된 파일(theme/static/css/dist)이 저장소에 들어 있다.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py runserver 0.0.0.0:8000"]
