# NEXT UPDATE:
# Second try if failed

#%%
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    StaleElementReferenceException,
    NoSuchElementException,
)
import time
import os
import signal
import json
import logging
from datetime import datetime
from random import uniform
import csv
from typing import Optional

#%%
# LOGGING SETUP
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# TYPE ALIASES
# A single definition block returned by the JS extractor.
#   {
#     "definition": str,        # e.g. "1) a school lesson"
#     "sentences":  list[str],  # example sentences / expressions
#   }
DefinitionBlock = dict[str, str | list[str]]
 
# A fully-parsed dictionary entry for one headword (one <h2.inline> block).
#   Keys: Lookup term, Translation, Dictionary entry, Part of speech,
#         Article, Pronunciation, Inflections, Definitions
DictionaryEntry = dict[str, str | list[DefinitionBlock] | None]

# %%
# EXTRACTION FUNCTIONS
def get_entry_data(
    driver: webdriver.Edge,
    word: str,
    h2,  # selenium WebElement — no stub import needed
) -> Optional[DictionaryEntry]:
    """Extract data for a single ``h2.inline`` block.
 
    Iterates over every piece of structured information attached to *h2*
    (translation title, part-of-speech, article, pronunciation, inflections,
    definitions with example sentences) and returns them as a flat dict.
 
    Parameters
    ----------
    driver:
        Active Selenium WebDriver instance.
    word:
        The search term that was typed into the dictionary (used as the
        "Lookup term" field in the returned dict).
    h2:
        The ``<h2 class="inline">`` WebElement that anchors this entry in the
        DOM.  All sibling traversal starts from this element.
 
    Returns
    -------
    DictionaryEntry | None
        Parsed entry dict, or ``None`` if an unexpected exception occurs
        during extraction.
    """
    try:
        # 1. LOOKUP TERM
        # The original search term is the "Lookup term" — not necessarily the same as the headword text (e.g. "lopen" → "loop").

        # 2. TRANSLATION TITLE
        # The section heading (e.g. "Dutch → English") lives in the nearest
        # preceding <div class="aaa"> sibling.
        try:
            translation_title: Optional[str] = h2.find_element(
                By.XPATH, "preceding-sibling::div[@class='aaa'][1]"
            ).text.strip()
        except NoSuchElementException:
            translation_title = None
 
        # 3. DICTIONARY ENTRY
        # The headword text is the h2 element itself — always present.
        dictionary_entry: str = h2.text.strip()
 
        # 4. PART OF SPEECH
        # Sits in the first non-empty text node immediately after the h2.
        part_of_speech: Optional[str] = driver.execute_script(
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
            """,
            h2,
        )
 
        # 5. ARTICLE
        # Encoded as a small <font style="font-size:8pt"> child inside the h2.
        # Returns "-" when no article is found (e.g. verbs, adjectives).
        article: str = driver.execute_script(
            """
            let fonts = arguments[0].querySelectorAll('font');
            for (let font of fonts) {
                if (font.style.fontSize === '8pt') {
                    return font.textContent.trim();
                }
            }
            return "-";
            """,
            h2,
        )
 
        # 6. PRONUNCIATION
        # Stored in the <td> that follows the "Uitspraak" label inside the
        # first sibling <table> after the h2.
        try:
            pronunciation: Optional[str] = h2.find_element(
                By.XPATH,
                "following-sibling::table[1]//td[@class='smallcaps']"
                "[contains(text(),'Uitspraak')]/following-sibling::td",
            ).text.strip()
        except NoSuchElementException:
            pronunciation = None
 
        # 7. INFLECTIONS
        # Same table structure as pronunciation but keyed on "Verbuigingen".
        # JS is used to normalise whitespace around closing parentheses.
        try:
            td = h2.find_element(
                By.XPATH,
                "following-sibling::table[1]//td[@class='smallcaps']"
                "[contains(text(),'Verbuigingen')]/following-sibling::td",
            )
            inflections: Optional[str] = driver.execute_script(
                r"""
                let td = arguments[0];
                let parts = [];
                td.childNodes.forEach(node => {
                    let text = node.textContent.trim();
                    if (text) parts.push(text);
                });
                return parts.join(" ").replace(/\s+\)/g, ")");
                """,
                td,
            )
        except NoSuchElementException:
            inflections = None
 
        # 8. DEFINITIONS
        # JS walks the DOM from h2 until it hits the next h2.inline or the
        # "Overige bronnen" section.  It collects numbered definition blocks,
        # each with optional example-sentence rows and expression pairs
        # (bold phrase + hidden translation div).
        # Returns [] when nothing is found — no try/except needed.
        definitions: list[dict] = driver.execute_script(
            r"""
            let start = arguments[0];
            let node = start.nextSibling;
            let results = [];
            let current = null;

            function flushCurrent() {
                if (current) {
                    results.push(current);
                    current = null;
                }
            }

            while (node) {
                // STOP at next h2.inline
                if (node.nodeType === 1 &&
                    node.tagName.toLowerCase() === "h2" &&
                    node.classList.contains("inline")) break;

                // STOP at "Overige bronnen" (other sources) section
                if (node.nodeType === 1 &&
                    node.tagName.toLowerCase() === "div" &&
                    node.classList.contains("aaa") &&
                    node.innerText.trim().includes("Overige bronnen")) break;

                if (node.nodeType === 1 && node.tagName.toLowerCase() === "font") {
                    let text = node.innerText.trim();

                    if (text.match(/^\d+\)$/)) {
                        // Numbered definition marker: "1)", "2)", "3)", …
                        flushCurrent();
                        current = { definition: text, sentences: [] };

                    } else if (text && !text.match(/^\s*$/)) {
                        // Definition body text — append to current block or
                        // start an unnumbered block (e.g. single-sense verbs).
                        if (!current) {
                            current = { definition: "", sentences: [] };
                        }
                        current.definition += (current.definition ? " " : "") + text;
                    }
                }

                // Plain text nodes between <font> tags (e.g. " - " separators)
                if (node.nodeType === 3 && current) {
                    let text = node.textContent.trim();
                    if (text === "-") current.definition += " - ";
                    else if (text) current.definition += " " + text;
                }

                if (node.nodeType === 1 && node.tagName.toLowerCase() === "table") {
                    let tableText = node.innerText.trim();

                    // Skip the pronunciation / inflection tables already handled above
                    if (tableText.includes("Uitspraak") || tableText.includes("Verbuigingen")) {
                        node = node.nextSibling;
                        continue;
                    }

                    if (current) {
                        node.querySelectorAll("tr").forEach(tr => {
                            // Expression rows: bold headword + hidden translation div
                            let boldFont = tr.querySelector("font[style*='color:#222']");
                            let hiddenDiv = tr.querySelector("div[style*='display']");

                            if (boldFont || hiddenDiv) {
                                let exprName = boldFont ? boldFont.innerText.trim() : "";
                                let exprTranslation = "";

                                if (hiddenDiv) {
                                    // Replace <br> with \n to preserve separate lines
                                    // e.g. "(=ter kennismaking) - introduction" on one line
                                    //      "introductiekorting - introduction sale" on next
                                    hiddenDiv.querySelectorAll("br").forEach(br => br.replaceWith("\n"));
                                    exprTranslation = hiddenDiv.innerText.trim().replace(/\n{2,}/g, "\n");
                                }

                                // Combine expression name + its translations
                                let sentence = exprName
                                    ? exprName + "\n" + exprTranslation
                                    : exprTranslation;
                                if (sentence.trim()) current.sentences.push(sentence.trim());

                            } else {
                                // Regular Dutch ↔ English example-sentence row
                                let pair = tr.innerText.trim();
                                if (pair) current.sentences.push(pair);
                            }
                        });
                    }
                }

                node = node.nextSibling;
            }

            flushCurrent();
            return results;
            """,
            h2,
        )
 
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
 
 
def get_word_data(driver: webdriver.Edge, word: str) -> list[DictionaryEntry]:
    """Fetch and extract all dictionary entries for a single *word*.
 
    Types the word into the search box, clicks "Vertaal", waits for the
    results page to load, then calls :func:`get_entry_data` for every
    ``h2.inline`` element found.
 
    Parameters
    ----------
    driver:
        Active Selenium WebDriver instance.
    word:
        Dutch word to look up.
 
    Returns
    -------
    list[DictionaryEntry]
        One dict per headword block found on the results page.
        Returns an empty list on timeout or unexpected error.
    """
    wait = WebDriverWait(driver, 10)
 
    try:
        search_box = wait.until(EC.presence_of_element_located((By.ID, "woord")))
        search_box.clear()
        search_box.send_keys(word)
 
        vertaal = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//a[contains(text(),'Vertaal')]"))
        )
        vertaal.click()
 
        h2_elements = wait.until(
            EC.presence_of_all_elements_located((By.CLASS_NAME, "inline"))
        )
        h2_count = len(h2_elements)
 
        results: list[DictionaryEntry] = []
        for idx in range(h2_count):
            try:
                # Re-query each time to avoid StaleElementReferenceException
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
 
 
def get_word_data_with_retry(
    driver: webdriver.Edge,
    word: str,
    max_retries: int = 3,
) -> list[DictionaryEntry]:
    """Wrap :func:`get_word_data` with exponential-ish back-off on transient failures.
 
    Parameters
    ----------
    driver:
        Active Selenium WebDriver instance.
    word:
        Dutch word to look up.
    max_retries:
        Maximum number of attempts before giving up and returning ``[]``.
 
    Returns
    -------
    list[DictionaryEntry]
        Extracted entries, or an empty list after all retries are exhausted.
    """
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
 
    return []  # unreachable, but satisfies type checkers

#%%
# SAVE FUNCTIONS
def save_as_txt(
    data: list[DictionaryEntry],
    filename: Optional[str] = None,
) -> str:
    """Save *data* as a human-readable plain-text file.
 
    Each entry is separated by a line of ``=`` characters.  Fields that are
    ``None`` or empty are silently omitted.
 
    Returns the path of the file that was written.
    """
    if filename is None:
        filename = f"dictionary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
 
    with open(filename, "w", encoding="utf-8") as f:
        for entry in data:
            f.write("=" * 70 + "\n")
            f.write(f"Lookup term: {entry['Lookup term']}\n")
            if entry["Translation"]:
                f.write(f"Translation: {entry['Translation']}\n")
            if entry["Dictionary entry"]:
                f.write(f"Dictionary entry: {entry['Dictionary entry']}\n")
            if entry["Part of speech"]:
                f.write(f"Part of Speech: {entry['Part of speech']}\n")
            if entry["Article"]:
                f.write(f"Article: {entry['Article']}\n")
            if entry["Pronunciation"]:
                f.write(f"Pronunciation: {entry['Pronunciation']}\n")
            if entry["Inflections"]:
                f.write(f"Inflections: {entry['Inflections']}\n")
 
            f.write("Definitions:\n")
            if entry["Definitions"]:
                for defn in entry["Definitions"]:
                    f.write(f"{defn['definition']}\n")
                    for sentence in defn["sentences"]:
                        # sentences may contain \n for expression name + translation
                        f.write(f"{sentence}\n")
                    f.write("\n")
            else:
                f.write("(No definitions found)\n\n")
 
    logger.info(f"✓ Saved {len(data)} entries to {filename}")
    return filename
 

def save_as_anki(
    data: list[DictionaryEntry],
    filename: str | None = None,
) -> str:
    """Save *data* as an Anki import file — **full vocabulary cards**.

    Column order (tab-separated, no header row):
        Dictionary entry | Part of Speech | Article | Pronunciation | Inflections | Definitions

    Front/Back field mapping is configured manually in Anki on import.

    Parameters
    ----------
    data:
        Parsed dictionary entries to export.
    filename:
        Output path.  Auto-generated from the current timestamp when omitted.

    Returns the path of the file that was written.
    """
    if filename is None:
        filename = f"dictionary_{datetime.now().strftime('%Y%m%d_%H%M%S')}_anki.txt"

    with open(filename, "w", encoding="utf-8") as f:
        for entry in data:
            # LOOKUP TERM
            lookup_term = entry["Lookup term"] or ""

            # DICTIONARY ENTRY
            dictionary_entry = entry["Dictionary entry"] or ""

            # PART OF SPEECH
            pos = entry["Part of speech"] or ""

            # ARTICLE
            article = entry["Article"] or ""

            # PRONUNCIATION
            pronunciation = entry["Pronunciation"] or ""

            # INFLECTIONS
            inflections = entry["Inflections"] or ""

            # DEFINITIONS — each definition block joined by <br>
            # example sentences indented with bullet point
            def_parts: list[str] = []
            if entry["Definitions"]:
                for defn in entry["Definitions"]:
                    if defn["definition"]:
                        # replace any newlines inside definition text itself
                        clean_defn = defn["definition"].replace("\n", " ")
                        def_parts.append(clean_defn + "<br>")
                    sentences = defn["sentences"]
                    for i, sentence in enumerate(sentences):
                        s = sentence.replace("\n", "<br>")
                        if i == len(sentences) - 1:
                            def_parts.append(s + "<br>")
                        else:
                            def_parts.append(s)
            definitions = "<br>".join(def_parts)

            f.write(
                f"{lookup_term}\t"
                f"{dictionary_entry}\t"
                f"{pos}\t"
                f"{article}\t"
                f"{pronunciation}\t"
                f"{inflections}\t"
                f"{definitions}\n"
            )

    logger.info(f"✓ Saved {len(data)} full vocab cards to {filename}")
    return filename
 

#%%
# MAIN SCRAPING FUNCTION
 
# All format strings accepted by the ``output_format`` parameter.
OutputFormat = str  # Literal["txt", "csv", "markdown", "json", "anki", "anki_full", "anki_articles"]
 
 
def scrape_dictionary(
    words: list[str],
    output_format: OutputFormat = "txt",
    output_dir: str | None = None,
    output_name: str | None = None,
) -> None:
    """Scrape Dutch–English dictionary entries and save them to disk.

    Opens a browser, sets the language pair to NL → EN, then iterates over
    *words*, calling :func:`get_word_data_with_retry` for each.  Collected
    entries are written in the chosen *output_format*.

    Parameters
    ----------
    words:
        Dutch words to look up.
    output_format:
        Serialisation format.  One of:

        * ``"txt"``  — plain text (human-readable)
        * ``"anki"`` — Anki import file

        Defaults to ``"txt"``.  Unknown values fall back to ``"txt"`` with a
        warning.
    output_dir:
        Directory to save the output file.  Defaults to current directory.
    output_name:
        Base filename without extension.  Defaults to timestamped name.
    """
    driver = None

    # Build output path from directory + name
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = output_name or f"dictionary_{ts}"
    directory = output_dir or ""
    output_file = os.path.join(directory, name) if directory else name

    # Create output directory if it does not exist
    if directory:
        os.makedirs(directory, exist_ok=True)
        logger.info(f"Output directory: {directory}")

    try:
        logger.info("Starting browser...")
        driver = webdriver.Edge()
        driver.get("https://www.mijnwoordenboek.nl/")

        wait = WebDriverWait(driver, 10)

        # Set language pair: NL (source) → EN (target)
        logger.info("Setting language to NL → EN...")
        Select(wait.until(EC.presence_of_element_located((By.ID, "src")))).select_by_value("NL")
        Select(driver.find_element(By.ID, "des")).select_by_value("EN")

        all_data: list[DictionaryEntry] = []
        stats: dict[str, int] = {
            "total_words": 0,
            "successful": 0,
            "failed": 0,
            "total_entries": 0,
        }

        for i, word in enumerate(words, 1):
            logger.info(f"[{i}/{len(words)}] Processing: '{word}'")
            data = get_word_data_with_retry(driver, word)

            stats["total_words"] += 1
            if data:
                all_data.extend(data)
                stats["successful"] += 1
                stats["total_entries"] += len(data)
                logger.info(f"  ✓ Found {len(data)} entry/entries")
            else:
                stats["failed"] += 1
                logger.warning(f"  ✗ No data found for '{word}'")

            # Polite delay between requests to avoid hammering the server
            if i < len(words):
                time.sleep(uniform(1, 3))

        # Save in the chosen format
        logger.info(f"Saving data as {output_format.upper()}...")

        if output_format == "txt":
            save_as_txt(all_data, output_file + ".txt")

        elif output_format == "anki":
            save_as_anki(all_data, filename=output_file + ".txt")

        else:
            logger.error(f"Unknown format '{output_format}'. Falling back to 'txt'.")
            save_as_txt(all_data, output_file + ".txt")

        # Summary
        logger.info("\n" + "=" * 70)
        logger.info("SCRAPING SUMMARY")
        logger.info("=" * 70)
        logger.info(f"Words processed:        {stats['total_words']}")
        logger.info(f"Successful:             {stats['successful']}")
        logger.info(f"Failed:                 {stats['failed']}")
        logger.info(f"Total entries extracted:{stats['total_entries']}")
        logger.info("=" * 70)

    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        if driver:
            try:
                driver.close()
                pid = driver.service.process.pid
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
            logger.info("Browser closed")

#%%
# ENTRY POINT
if __name__ == "__main__":
    words_file  = r"Woorden\Les 1 Woorden.txt"   # ← input
    output_file = r"Resultaat"  # ← output (no extension, anki adds its own)

    with open(words_file, "r", encoding="utf-8") as f:
        words = [line.strip() for line in f if line.strip()]

    # output_format options:
    #   "txt"           → plain-text file
    #   "anki"          → Anki deck
    scrape_dictionary(words,
                      output_format = "anki",
                      output_dir    = r"Resultaat",
                      output_name   = "ETC Anki Unit 1")

# %%

