FROM mcr.microsoft.com/azure-functions/python:4-python3.12

ENV AzureWebJobsScriptRoot=/home/site/wwwroot \
    AzureFunctionsJobHost__Logging__Console__IsEnabled=true \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /home/site/wwwroot

COPY . /home/site/wwwroot

# Install system dependencies required by skills (pandoc for docx/pptx conversion,
# libreoffice-calc for xlsx recalculation, poppler-utils for pdf2image)
# and CJK fonts for Chinese/Japanese/Korean text in PDF/DOCX/PPTX/XLSX output.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       pandoc \
       libreoffice-calc \
       poppler-utils \
       fonts-noto-cjk \
       fonts-noto-cjk-extra \
       fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -fv

# Install Python dependencies, then install the Copilot CLI package.
# The app relies on package.json in the app root copied from infra/assets.
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt \
    && if ! command -v npm >/dev/null 2>&1; then apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*; fi \
    && npm install --os=linux --cpu=x64 --omit=dev
