# discourse-evaluation-system/Dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies and Perl
RUN apt-get update && apt-get install -y \
    perl \
    cpanminus \
    make \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire application into the container
COPY . .

# Install required Perl modules
WORKDIR /app/scorer
RUN cpanm --notest \
    JSON \
    File::Slurp \
    Algorithm::Munkres \
    Math::Combinatorics

# Return to app root
WORKDIR /app

# Expose FastAPI port
EXPOSE 8000

# Default command
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
