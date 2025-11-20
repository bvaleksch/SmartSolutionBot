# Base image with Python 3 (внутри 3.11, снаружи вы просто используете python3)
FROM python:3.11-slim

# Do not write .pyc files and use unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies (build tools are often required by ML libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set working directory where submissions will be mounted
WORKDIR /workspace

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Install common Python libraries (without torch for now)
RUN pip install --no-cache-dir \
    numpy \
    pandas \
    scikit-learn \
    nltk \
    transformers \
    sentence-transformers \
    chromadb \
    faiss-cpu

# Install CPU-only PyTorch and torchvision from the official CPU index
# (No CUDA, подходит для авто-оценки на CPU)
RUN pip install --no-cache-dir \
    torch \
    torchvision \
    --index-url https://download.pytorch.org/whl/cpu

# Pre-download NLTK data useful for Russian
# This step выполняется на этапе сборки (когда есть интернет),
# в рантайме доступ к сети уже не нужен.
RUN python3 -m nltk.downloader \
    punkt \
    stopwords \
    snowball_data \
    averaged_perceptron_tagger \
    averaged_perceptron_tagger_ru \
    universal_tagset \
    wordnet \
    omw-1.4 \
    nonbreaking_prefixes

# Optional: default command (not required for auto-judge)
CMD ["python3"]