#!/bin/bash

# ==============================================================================
# USER CONFIGURATION: Update these versions as needed for future releases
# ==============================================================================
OCP_VER="4.21"
GITOPS_VER="1.19"
ODF_VER="4.21"
ACM_VER="2.16"

# General Settings
USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
DELAY=1 # Delay between downloads to avoid IP blocks (in seconds)
# ==============================================================================

# --- PRE-REQUISITE CHECK ---
if ! command -v pdfunite >/dev/null 2>&1; then
    echo "❌ Error: pdfunite not found. Run: sudo dnf install poppler-utils -y"
    exit 1
fi

# --- THE DYNAMIC CORE FUNCTION ---
process_product() {
    local PROD=$1
    local VER=$2
    local FINAL_PDF="Master_${PROD^^}_${VER}.pdf" # Generates name like Master_OCP_4.21.pdf
    local BASE_URL="https://docs.redhat.com/en/documentation/$PROD/$VER"
    local TEMP_DIR="temp_${PROD}_${VER}"
    local VALID_FILES=()

    echo "----------------------------------------------------"
    echo "🔍 DYNAMICALLY MAPPING: $PROD v$VER"
    echo "----------------------------------------------------"
    
    # 1. Scrape the landing page for valid "Book" slugs
    echo "📡 Fetching guide list from $BASE_URL..."
    local SLUGS=$(curl -s -L -A "$USER_AGENT" "$BASE_URL" | \
        grep -oP "$PROD/$VER/html/\K[^/\"' ]+" | \
        sort -u)

    if [ -z "$SLUGS" ]; then
        echo "⚠️  No documents found for $PROD $VER. Check if version exists."
        return
    fi

    echo "📂 Found $(echo "$SLUGS" | wc -l) unique guides. Downloading..."
    mkdir -p "$TEMP_DIR" && cd "$TEMP_DIR"

    # 2. Download loop
    for SLUG in $SLUGS; do
        echo "📥 Fetching: $SLUG"
        
        # Use -L to follow redirects and -O for unique filenames
        wget -q -L -O "${SLUG}.pdf" --user-agent="$USER_AGENT" \
            "https://docs.redhat.com/en/documentation/$PROD/$VER/pdf/$SLUG/"
        
        # 3. Validation: Only keep files > 35KB (Filters out 404/403 HTML pages)
        if [ -s "${SLUG}.pdf" ] && [ $(stat -c%s "${SLUG}.pdf") -gt 35000 ]; then
            VALID_FILES+=("${SLUG}.pdf")
        else
            rm -f "${SLUG}.pdf"
        fi
        sleep $DELAY
    done

    # 4. Merge
    if [ ${#VALID_FILES[@]} -gt 0 ]; then
        echo "Merging ${#VALID_FILES[@]} books into $FINAL_PDF..."
        pdfunite "${VALID_FILES[@]}" "../$FINAL_PDF"
        echo "✅ SUCCESS: $FINAL_PDF created."
    else
        echo "❌ ERROR: No valid PDF volumes found for $PROD."
    fi

    # 5. Purge individual PDFs
    cd .. && rm -rf "$TEMP_DIR"
    echo ""
}

# --- MAIN EXECUTION ---

# Process OpenShift Container Platform
process_product "openshift_container_platform" "$OCP_VER" "Master_OCP_$OCP_VER.pdf"

# Process OpenShift GitOps
process_product "red_hat_openshift_gitops" "$GITOPS_VER" "Master_GitOps_$GITOPS_VER.pdf"

# Process OpenShift Data Foundation
process_product "red_hat_openshift_data_foundation" "$ODF_VER" "Master_ODF_$ODF_VER.pdf"

# Process Advanced Cluster Management
process_product "red_hat_advanced_cluster_management_for_kubernetes" "$ACM_VER" "Master_ACM_$ACM_VER.pdf"

echo "----------------------------------------------------"
echo "🌟 ALL PROCESSES COMPLETE"
ls -lh Master_*.pdf
