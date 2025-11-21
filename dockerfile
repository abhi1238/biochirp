FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app

EXPOSE 8010

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010",  "--workers", "10"]





# FROM python:3.11-slim

# # Create a non-root user
# RUN useradd -m myuser

# WORKDIR /app
# COPY requirements.txt requirements.txt
# # COPY resources resources
# RUN pip install --no-cache-dir -r requirements.txt
# COPY app ./app

# # Make sure log dir exists and is owned by myuser
# RUN mkdir -p /app/logs && chown myuser:myuser /app/logs

# # Make sure files are owned by myuser (so myuser can read/write if needed)
# RUN chown -R myuser:myuser /app

# RUN chmod 775 /app/logs

# # Switch to non-root user
# USER myuser

# ENV PYTHONPATH=/app

# EXPOSE 8010

# CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010",  "--workers", "4"]

