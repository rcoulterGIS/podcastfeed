FROM python:3.12-slim

WORKDIR /app

# Install Flask
RUN pip install --no-cache-dir flask

# Copy application
COPY app.py .

# Create data directory for SQLite
RUN mkdir -p /data

# Expose port
EXPOSE 8080

# Run application
CMD ["python", "app.py"]
