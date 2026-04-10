#%%
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, NoSuchElementException
import time
import json
import logging
from datetime import datetime
from random import uniform
import csv

#%%
# LOGGING SETUP
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# %%
# EXTRACTION FUNCTIONS
def get_entry_data(driver, word, h2):
    """Extract data for a single h2.inline block. Returns None for missing fields."""

    try:
        # 1. TRANSLATION TITLE
        try:
            translation_title = h2.find_element(
                By.XPATH, "preceding-sibling::div[@class='aaa'][1]"
            ).text.strip()
        except NoSuchElementException:
            translation_title = None

        # 2. DICTIONARY ENTRY
        dictionary_entry = h2.text.strip()  # h2 itself — safe, always present

        # 3A. PART OF SPEECH
        part_of_speech = driver.execute_script(
            """
            let el = arguments[0];
            let node = el.nextSibling;
            while (node) {
                if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                    return node.textContent.trim();
                }
                node = node.nextSibling;
            }
            return null;
            """, h2)  # JS already returns null if not found — no try/except needed
        
        # 3B. ARTICLE
        article = driver.execute_script(
            """
            let fonts = arguments[0].querySelectorAll('font');
            for (let font of fonts) {
                if (font.style.fontSize === '8pt') {
                    return font.textContent.trim();
                }
            }
            return "-";
            """, h2)

        # 4. PRONUNCIATION
        try:
            pronunciation = h2.find_element(
                By.XPATH,
                "following-sibling::table[1]//td[@class='smallcaps']"
                "[contains(text(),'Uitspraak')]/following-sibling::td"
            ).text.strip()
        except NoSuchElementException:
            pronunciation = None

        # 5. INFLECTIONS
        try:
            td = h2.find_element(
                By.XPATH,
                "following-sibling::table[1]//td[@class='smallcaps']"
                "[contains(text(),'Verbuigingen')]/following-sibling::td"
            )
            inflections = driver.execute_script(
                """
                let td = arguments[0];
                let parts = [];
                td.childNodes.forEach(node => {
                    let text = node.textContent.trim();
                    if (text) parts.push(text);
                });
                return parts.join(" ").replace(/\\s+\\)/g, ")");
                """, td)
        except NoSuchElementException:
            inflections = None

        # 6. DEFINITIONS (JS already returns [] if nothing found — no try/except needed)
        definitions = driver.execute_script(
            """
            let start = arguments[0];
            let node = start.nextSibling;
            let results = [];
            let current = null;

            while (node) {
                // STOP at next h2.inline
                if (node.nodeType === 1 &&
                    node.tagName.toLowerCase() === "h2" &&
                    node.classList.contains("inline")) break;

                // STOP at "Overige bronnen"
                if (node.nodeType === 1 &&
                    node.tagName.toLowerCase() === "div" &&
                    node.classList.contains("aaa") &&
                    node.innerText.trim().includes("Overige bronnen")) break;

                // DEFINITION NUMBER & TEXT
                if (node.nodeType === 1 && node.tagName.toLowerCase() === "font") {
                    let text = node.innerText.trim();
                    
                    if (text.match(/^\d+\)$/)) {
                        if (current) results.push(current);
                        current = { definition: text, sentences: [] };
                    } else if (current && text) {
                        current.definition += " " + text;
                    }
                }

                // Text node between fonts
                if (node.nodeType === 3 && current) {
                    let text = node.textContent.trim();
                    if (text && text !== "-") current.definition += " " + text;
                }

                // SENTENCE TABLE
                if (node.nodeType === 1 && node.tagName.toLowerCase() === "table") {
                    let text = node.innerText.trim();
                    
                    if (text.includes("Uitspraak") || text.includes("Verbuigingen")) {
                        node = node.nextSibling;
                        continue;
                    }
                    
                    if (current) {
                        node.querySelectorAll("tr").forEach(tr => {
                            let pair = tr.innerText.trim();
                            if (pair) {
                                current.sentences.push(pair);
                            }
                        });
                        
                        // ALSO CHECK FOR HIDDEN DIVS INSIDE THIS TABLE
                        let hiddenDivs = node.querySelectorAll("div[style*='display: none']");
                        hiddenDivs.forEach(div => {
                            let fonts = div.querySelectorAll("font");
                            let hiddenText = "";
                            fonts.forEach((font, idx) => {
                                let fontText = font.innerText.trim();
                                if (fontText) {
                                    if (idx === 0) hiddenText = fontText;
                                    else if (idx === 1) hiddenText += " - " + fontText;
                                    else hiddenText += " " + fontText;
                                }
                            });
                            if (hiddenText) {
                                current.sentences.push(hiddenText);
                            }
                        });
                    }
                }

                node = node.nextSibling;
            }

            if (current) results.push(current);
            return results;
            """, h2)

        return {
            "Lookup term": word,
            "Translation": translation_title,
            "Dictionary entry": dictionary_entry,
            "Part of speech": part_of_speech,
            "Article": article,
            "Pronunciation": pronunciation,
            "Inflections": inflections,
            "Definitions": definitions,
        }
    
    except Exception as e:
        logger.error(f"Error extracting entry data for '{word}': {e}")
        return None

def get_word_data(driver, word):
    """Fetch and extract all entries for a single word."""
    wait = WebDriverWait(driver, 10)

    try:
        # Input word
        search_box = wait.until(EC.presence_of_element_located((By.ID, "woord")))
        search_box.clear()
        search_box.send_keys(word)

        # Click "Vertaal"
        vertaal = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(),'Vertaal')]")))
        vertaal.click()

        # Wait for result title and get count
        h2_elements = wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "inline")))
        h2_count = len(h2_elements)
        
        results = []
        for idx in range(h2_count):
            try:
                # Re-fetch each element fresh to avoid stale references
                h2 = driver.find_elements(By.CLASS_NAME, "inline")[idx]
                entry = get_entry_data(driver, word, h2)
                if entry:
                    results.append(entry)
            except (StaleElementReferenceException, IndexError):
                logger.warning(f"Skipped h2 element at index {idx} for '{word}'")
                continue
        
        return results
    
    except TimeoutException:
        logger.warning(f"Timeout while processing '{word}'")
        return []
    except Exception as e:
        logger.error(f"Error processing '{word}': {e}")
        return []
    
    except TimeoutException:
        logger.warning(f"Timeout while processing '{word}'")
        return []
    except Exception as e:
        logger.error(f"Error processing '{word}': {e}")
        return []

def get_word_data_with_retry(driver, word, max_retries=3):
    """Retry extraction on transient failures."""
    for attempt in range(1, max_retries + 1):
        try:
            return get_word_data(driver, word)
        except (StaleElementReferenceException, TimeoutException):
            if attempt < max_retries:
                logger.warning(f"Retry {attempt}/{max_retries} for '{word}'")
                time.sleep(2)
            else:
                logger.error(f"Failed after {max_retries} attempts for '{word}'")
                return []
        except Exception as e:
            logger.error(f"Error on attempt {attempt}: {e}")
            return []

#%%
# SAVE FUNCTIONS
def save_as_txt(data, filename=None):
    """Save dictionary data in simple format."""
    if filename is None:
        filename = f"dictionary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    
    with open(filename, 'w', encoding='utf-8') as f:
        for entry in data:
            f.write("=" * 70 + "\n")
            f.write(f"Lookup term: {entry['Lookup term']}\n") 

            if entry['Translation']:
                f.write(f"Translation: {entry['Translation']}\n")
            if entry['Dictionary entry']:
                f.write(f"Dictionary entry: {entry['Dictionary entry']}\n")
            if entry['Part of speech']:
                f.write(f"Part of Speech: {entry['Part of speech']}\n")
            if entry['Article']:
                f.write(f"Article: {entry['Article']}\n")
            if entry['Pronunciation']:
                f.write(f"Pronunciation: {entry['Pronunciation']}\n")
            if entry['Inflections']:
                f.write(f"Inflections: {entry['Inflections']}\n")
            
            f.write("Definitions:\n")
            if entry['Definitions']:
                for defn in entry['Definitions']:
                    f.write(f"{defn['definition']}\n")
                    if defn['sentences']:
                        for sentence in defn['sentences']:
                            f.write(f"{sentence}\n")
                    f.write("\n")
            else:
                f.write("(No definitions found)\n\n")
    
    logger.info(f"✓ Saved {len(data)} entries to {filename}")
    return filename

def save_as_csv_txt(data, filename=None):
    """Save as tab-separated CSV file."""
    if filename is None:
        filename = f"dictionary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    with open(filename, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Word", "Translation Title", "Word Function", "Part of Speech", "Pronunciation", "Inflections", "Definition", "Example Sentences"])
        
        for entry in data:
            word = entry['Word']
            trans_title = entry['Translation title'] or ""
            word_func = entry['Word function'] or ""
            pos = entry['Part of speech'] or ""
            pron = entry['Pronunciation'] or ""
            inflect = entry['Inflections'] or ""
            
            if entry['Definitions']:
                for defn in entry['Definitions']:
                    definition = defn['definition']
                    examples = " | ".join(defn['sentences']) if defn['sentences'] else ""
                    writer.writerow([word, trans_title, word_func, pos, pron, inflect, definition, examples])
            else:
                writer.writerow([word, trans_title, word_func, pos, pron, inflect, "(No definitions)", ""])
    
    logger.info(f"✓ Saved {len(data)} entries to {filename}")
    return filename

def save_as_markdown_txt(data, filename=None):
    """Save as markdown-style text file."""
    if filename is None:
        filename = f"dictionary_{datetime.now().strftime('%Y%m%d_%H%M%S')}_markdown.txt"
    
    with open(filename, 'w', encoding='utf-8') as f:
        f.write("# Dictionary Export\n\n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**Total entries:** {len(data)}\n\n")
        f.write("---\n\n")
        
        for entry in data:
            f.write(f"## {entry['Word']}\n\n")
            
            if entry['Translation title']:
                f.write(f"**Translation:** {entry['Translation title']}\n\n")
            if entry['Word function']:
                f.write(f"**Function:** {entry['Word function']}\n\n")
            if entry['Part of speech']:
                f.write(f"**Part of Speech:** {entry['Part of speech']}\n\n")
            if entry['Pronunciation']:
                f.write(f"**Pronunciation:** {entry['Pronunciation']}\n\n")
            if entry['Inflections']:
                f.write(f"**Inflections:** {entry['Inflections']}\n\n")
            
            f.write("### Definitions\n\n")
            if entry['Definitions']:
                for defn in entry['Definitions']:
                    f.write(f"- {defn['definition']}\n")
                    if defn['sentences']:
                        for sentence in defn['sentences']:
                            f.write(f"  - {sentence}\n")
                    f.write("\n")
            else:
                f.write("- (No definitions found)\n\n")
            
            f.write("---\n\n")
    
    logger.info(f"✓ Saved {len(data)} entries to {filename}")
    return filename

def save_as_json(data, filename=None):
    """Save as JSON format."""
    if filename is None:
        filename = f"dictionary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"✓ Saved {len(data)} entries to {filename}")
    return filename

#%%
# MAIN SCRAPING FUNCTION
def scrape_dictionary(words, output_format='txt', output_file=None):
    """
    Scrape Dutch-English dictionary entries.
    
    Args:
        words (list): List of words to scrape
        output_format (str): Format to save ('txt', 'csv', 'markdown', 'json')
        output_file (str): Custom output filename (optional)
    """
    driver = None
    try:
        logger.info("Starting browser...")
        driver = webdriver.Edge()
        driver.get("https://www.mijnwoordenboek.nl/")
        
        wait = WebDriverWait(driver, 10)
        
        # Set language NL → EN
        logger.info("Setting language to NL → EN...")
        Select(wait.until(EC.presence_of_element_located((By.ID, "src")))).select_by_value("NL")
        Select(driver.find_element(By.ID, "des")).select_by_value("EN")
        
        all_data = []
        stats = {
            'total_words': 0,
            'successful': 0,
            'failed': 0,
            'total_entries': 0,
        }
        
        for i, word in enumerate(words, 1):
            logger.info(f"[{i}/{len(words)}] Processing: '{word}'")
            data = get_word_data_with_retry(driver, word)
            
            stats['total_words'] += 1
            if data:
                all_data.extend(data)
                stats['successful'] += 1
                stats['total_entries'] += len(data)
                logger.info(f"  ✓ Found {len(data)} entry/entries")
            else:
                stats['failed'] += 1
                logger.warning(f"  ✗ No data found for '{word}'")
            
            time.sleep(uniform(1, 3))
        
        # Save in chosen format
        logger.info(f"Saving data as {output_format.upper()}...")
        if output_format == 'txt':
            save_as_txt(all_data, output_file)
        elif output_format == 'csv':
            save_as_csv_txt(all_data, output_file)
        elif output_format == 'markdown':
            save_as_markdown_txt(all_data, output_file)
        elif output_format == 'json':
            save_as_json(all_data, output_file)
        else:
            logger.error(f"Unknown format: {output_format}. Using txt.")
            save_as_txt(all_data, output_file)
        
        # Print statistics
        logger.info("\n" + "=" * 70)
        logger.info("SCRAPING SUMMARY")
        logger.info("=" * 70)
        logger.info(f"Words processed: {stats['total_words']}")
        logger.info(f"Successful: {stats['successful']}")
        logger.info(f"Failed: {stats['failed']}")
        logger.info(f"Total entries extracted: {stats['total_entries']}")
        logger.info("=" * 70)
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        if driver:
            driver.quit()
            logger.info("Browser closed")

#%%
# MAIN
if __name__ == "__main__":
    # Words to scrape
    # words = ["jongen"]
    words = ["vrouw"]
    # words = ["jongen", "huis", "water"]
    
    # Now uses the simple format by default
    scrape_dictionary(words, output_format='txt')

# %%

