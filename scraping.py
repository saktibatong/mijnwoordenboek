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
        # 1. TRANSLATION TITLE
        # The section heading (e.g. "Dutch → English") lives in the nearest
        # preceding <div class="aaa"> sibling.
        try:
            translation_title: Optional[str] = h2.find_element(
                By.XPATH, "preceding-sibling::div[@class='aaa'][1]"
            ).text.strip()
        except NoSuchElementException:
            translation_title = None
 
        # 2. DICTIONARY ENTRY
        # The headword text is the h2 element itself — always present.
        dictionary_entry: str = h2.text.strip()
 
        # 3A. PART OF SPEECH
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
 
        # 3B. ARTICLE
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
 
        # 4. PRONUNCIATION
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
 
        # 5. INFLECTIONS
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
 
        # 6. DEFINITIONS
        # JS walks the DOM from h2 until it hits the next h2.inline or the
        # "Overige bronnen" section.  It collects numbered definition blocks,
        # each with optional example-sentence rows and expression pairs
        # (bold phrase + hidden translation div).
        # Returns [] when nothing is found — no try/except needed.
        definitions: list[DefinitionBlock] = driver.execute_script(
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
                            let hiddenDiv = tr.querySelector("div[style*='display: none']");
 
                            if (boldFont || hiddenDiv) {
                                let exprName = boldFont ? boldFont.innerText.trim() : "";
                                let exprTranslation = "";
 
                                if (hiddenDiv) {
                                    let fonts = hiddenDiv.querySelectorAll("font");
                                    let parts = [];
                                    fonts.forEach(f => {
                                        let t = f.innerText.trim();
                                        if (t) parts.push(t);
                                    });
                                    exprTranslation = parts.join(" - ");
                                }
 
                                let sentence = exprName;
                                if (exprTranslation) sentence += "\n" + exprTranslation;
                                if (sentence) current.sentences.push(sentence);
 
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
 
 
def save_as_csv_txt(
    data: list[DictionaryEntry],
    filename: Optional[str] = None,
) -> str:
    """Save *data* as a tab-separated CSV file.
 
    Each definition block becomes its own row; example sentences are joined
    with " | " in the last column.
 
    Returns the path of the file that was written.
    """
    if filename is None:
        filename = f"dictionary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
 
    with open(filename, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(
            [
                "Lookup term",
                "Translation",
                "Dictionary entry",
                "Part of speech",
                "Article",
                "Pronunciation",
                "Inflections",
                "Definition",
                "Example Sentences",
            ]
        )
 
        for entry in data:
            lookup   = entry["Lookup term"] or ""
            trans    = entry["Translation"] or ""
            dict_ent = entry["Dictionary entry"] or ""
            pos      = entry["Part of speech"] or ""
            article  = entry["Article"] or ""
            pron     = entry["Pronunciation"] or ""
            inflect  = entry["Inflections"] or ""
 
            if entry["Definitions"]:
                for defn in entry["Definitions"]:
                    definition = defn["definition"]
                    examples = (
                        " | ".join(defn["sentences"]) if defn["sentences"] else ""
                    )
                    writer.writerow(
                        [lookup, trans, dict_ent, pos, article, pron, inflect, definition, examples]
                    )
            else:
                writer.writerow(
                    [lookup, trans, dict_ent, pos, article, pron, inflect, "(No definitions)", ""]
                )
 
    logger.info(f"✓ Saved {len(data)} entries to {filename}")
    return filename
 
 
def save_as_markdown_txt(
    data: list[DictionaryEntry],
    filename: Optional[str] = None,
) -> str:
    """Save *data* as a Markdown document.
 
    Each headword becomes an ``##`` section.  Definitions and example
    sentences are rendered as nested bullet lists.
 
    Returns the path of the file that was written.
    """
    if filename is None:
        filename = f"dictionary_{datetime.now().strftime('%Y%m%d_%H%M%S')}_markdown.txt"
 
    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Dictionary Export\n\n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**Total entries:** {len(data)}\n\n")
        f.write("---\n\n")
 
        for entry in data:
            f.write(f"## {entry['Dictionary entry']}\n\n")
 
            if entry["Translation"]:
                f.write(f"**Translation:** {entry['Translation']}\n\n")
            if entry["Lookup term"]:
                f.write(f"**Lookup term:** {entry['Lookup term']}\n\n")
            if entry["Part of speech"]:
                f.write(f"**Part of Speech:** {entry['Part of speech']}\n\n")
            if entry["Article"]:
                f.write(f"**Article:** {entry['Article']}\n\n")
            if entry["Pronunciation"]:
                f.write(f"**Pronunciation:** {entry['Pronunciation']}\n\n")
            if entry["Inflections"]:
                f.write(f"**Inflections:** {entry['Inflections']}\n\n")
 
            f.write("### Definitions\n\n")
            if entry["Definitions"]:
                for defn in entry["Definitions"]:
                    f.write(f"- {defn['definition']}\n")
                    if defn["sentences"]:
                        for sentence in defn["sentences"]:
                            f.write(f"  - {sentence}\n")
                    f.write("\n")
            else:
                f.write("- (No definitions found)\n\n")
 
            f.write("---\n\n")
 
    logger.info(f"✓ Saved {len(data)} entries to {filename}")
    return filename
 
 
def save_as_json(
    data: list[DictionaryEntry],
    filename: Optional[str] = None,
) -> str:
    """Save *data* as a pretty-printed JSON file (UTF-8, 2-space indent).
 
    Returns the path of the file that was written.
    """
    if filename is None:
        filename = f"dictionary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
 
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
 
    logger.info(f"✓ Saved {len(data)} entries to {filename}")
    return filename
 

# ANKI EXPORT HELPERS
# Part-of-speech strings that indicate a noun entry.
# Extend this set if you encounter other labels on the target site.
NOUN_POS_MARKERS: frozenset[str] = frozenset({"zelfstandig naamwoord", "znw", "zn"})
 
 
def _is_noun(entry: DictionaryEntry) -> bool:
    """Return ``True`` if the entry's part-of-speech field indicates a noun."""
    pos = (entry["Part of speech"] or "").strip().lower()
    return any(marker in pos for marker in NOUN_POS_MARKERS)
 
 
def save_as_anki_full(
    data: list[DictionaryEntry],
    filename: Optional[str] = None,
    deck_tag: str = "dutch::full",
) -> str:
    """Save *data* as an Anki import file — **full vocabulary cards**.
 
    Card layout
    -----------
    Front:
        ``[article] headword  (part-of-speech)  [pronunciation]``
    Back:
        Translation, inflections, definitions, and up to 2 example sentences
        per definition.
 
    The file uses tab-separated columns compatible with Anki's plain-text
    importer (Basic note type).  Newlines inside fields are encoded as
    ``<br>`` HTML tags.
 
    Parameters
    ----------
    data:
        Parsed dictionary entries to export.
    filename:
        Output path.  Auto-generated from the current timestamp when omitted.
    deck_tag:
        Anki deck name written to the ``#deck:`` header line.
 
    Returns the path of the file that was written.
    """
    if filename is None:
        filename = f"dictionary_{datetime.now().strftime('%Y%m%d_%H%M%S')}_anki_full.txt"
 
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#separator:tab\n")
        f.write("#html:false\n")
        f.write(f"#deck:{deck_tag}\n")
        f.write("#notetype:Basic\n")
        f.write("#columns:Front\tBack\tTags\n")
 
        for entry in data:
            # RONT
            word    = entry["Dictionary entry"] or entry["Lookup term"] or ""
            article = entry["Article"]
            pos     = entry["Part of speech"]
            pron    = entry["Pronunciation"]
 
            # Prepend article only when it is a real article (not "-" placeholder)
            header = f"{article} {word}".strip() if article and article != "-" else word
            if pos:
                header += f"  ({pos})"
            if pron:
                header += f"  [{pron}]"
 
            front: str = header
 
            # BACK
            back_parts: list[str] = []
 
            if entry["Translation"]:
                back_parts.append(entry["Translation"])
            if entry["Inflections"]:
                back_parts.append(f"Inflections: {entry['Inflections']}")
            if entry["Definitions"]:
                for defn in entry["Definitions"]:
                    if defn["definition"]:
                        back_parts.append(defn["definition"])
                    # Limit to 2 example sentences per definition to keep cards concise
                    for sentence in defn["sentences"][:2]:
                        back_parts.append(f"  • {sentence}")
 
            back: str = "\n".join(back_parts)
 
            # TAGS
            lookup_tag = (entry["Lookup term"] or "").replace(" ", "_")
            tags = f"{deck_tag} {lookup_tag}".strip()
 
            # Anki uses <br> for in-field line breaks in plain-text imports
            f.write(
                f"{front.replace(chr(10), '<br>')}\t"
                f"{back.replace(chr(10), '<br>')}\t"
                f"{tags}\n"
            )
 
    logger.info(f"✓ Saved {len(data)} full vocab cards to {filename}")
    return filename
 
 
def save_as_anki_articles(
    data: list[DictionaryEntry],
    filename: Optional[str] = None,
    deck_tag: str = "dutch::articles",
) -> str:
    """Save noun entries as Anki **article drill cards**.
 
    Card layout
    -----------
    Front:
        Bare Dutch headword (no article).
    Back:
        ``article + headword``  (e.g. "het huis").
 
    Non-noun entries and entries without a valid article are silently skipped.
 
    Parameters
    ----------
    data:
        Parsed dictionary entries to export.
    filename:
        Output path.  Auto-generated from the current timestamp when omitted.
    deck_tag:
        Anki deck name written to the ``#deck:`` header line.
 
    Returns the path of the file that was written.
    """
    if filename is None:
        filename = (
            f"dictionary_{datetime.now().strftime('%Y%m%d_%H%M%S')}_anki_articles.txt"
        )
 
    # Only noun entries that carry a real article qualify for article drill
    nouns = [
        e for e in data if _is_noun(e) and e["Article"] and e["Article"] != "-"
    ]
 
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#separator:tab\n")
        f.write("#html:false\n")
        f.write(f"#deck:{deck_tag}\n")
        f.write("#notetype:Basic\n")
        f.write("#columns:Front\tBack\tTags\n")
 
        for entry in nouns:
            word    = entry["Dictionary entry"] or entry["Lookup term"] or ""
            article = entry["Article"]
 
            front: str = word
            back: str  = f"{article} {word}"
 
            lookup_tag = (entry["Lookup term"] or "").replace(" ", "_")
            tags = f"{deck_tag} {lookup_tag}".strip()
 
            f.write(f"{front}\t{back}\t{tags}\n")
 
    logger.info(
        f"✓ Saved {len(nouns)} article drill cards to {filename} "
        f"({len(data) - len(nouns)} non-noun entries skipped)"
    )
    return filename
 
 
def save_as_anki_both(
    data: list[DictionaryEntry],
    base_filename: Optional[str] = None,
    deck_prefix: str = "dutch",
) -> tuple[str, str]:
    """Convenience wrapper — saves both Anki decks in one call.
 
    Parameters
    ----------
    data:
        Parsed dictionary entries to export.
    base_filename:
        Optional base path stem.  When provided, the two files are named
        ``<base_filename>`` and ``<base_filename>_anki_articles.txt``.
        When omitted, auto-generated timestamps are used.
    deck_prefix:
        Prefix for both deck tags (e.g. ``"dutch"`` → ``"dutch::full"`` and
        ``"dutch::articles"``).
 
    Returns
    -------
    tuple[str, str]
        Paths of the full-vocab file and the articles-drill file respectively.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
 
    full_file     = base_filename or f"dictionary_{ts}_anki_full.txt"
    articles_file = (
        f"{base_filename}_anki_articles.txt"
        if base_filename
        else f"dictionary_{ts}_anki_articles.txt"
    )
 
    save_as_anki_full(data,     filename=full_file,     deck_tag=f"{deck_prefix}::full")
    save_as_anki_articles(data, filename=articles_file, deck_tag=f"{deck_prefix}::articles")
 
    return full_file, articles_file

#%%
# MAIN SCRAPING FUNCTION
 
# All format strings accepted by the ``output_format`` parameter.
OutputFormat = str  # Literal["txt", "csv", "markdown", "json", "anki", "anki_full", "anki_articles"]
 
 
def scrape_dictionary(
    words: list[str],
    output_format: OutputFormat = "txt",
    output_file: Optional[str] = None,
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
 
        * ``"txt"``           — plain text (human-readable)
        * ``"csv"``           — tab-separated CSV
        * ``"markdown"``      — Markdown document
        * ``"json"``          — JSON array
        * ``"anki"``          — both Anki decks (full vocab + article drill)
        * ``"anki_full"``     — Anki full-vocab deck only
        * ``"anki_articles"`` — Anki article-drill deck only (nouns only)
 
        Defaults to ``"txt"``.  Unknown values fall back to ``"txt"`` with a
        warning.
    output_file:
        Custom output filename / path stem.  When ``None`` a timestamped name
        is generated automatically.
    """
    driver = None
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
            save_as_txt(all_data, output_file)
 
        elif output_format == "csv":
            save_as_csv_txt(all_data, output_file)
 
        elif output_format == "markdown":
            save_as_markdown_txt(all_data, output_file)
 
        elif output_format == "json":
            save_as_json(all_data, output_file)
 
        elif output_format == "anki":
            # Exports both decks: full-vocab + article-drill
            save_as_anki_both(all_data, base_filename=output_file)
 
        elif output_format == "anki_full":
            # Full-vocabulary deck only
            save_as_anki_full(all_data, filename=output_file)
 
        elif output_format == "anki_articles":
            # Article-drill deck only (nouns with a valid article)
            save_as_anki_articles(all_data, filename=output_file)
 
        else:
            logger.error(f"Unknown format '{output_format}'. Falling back to 'txt'.")
            save_as_txt(all_data, output_file)
 
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
    words = [
        "Les",
        "Introductie",
        "Tekst",
        "Luisteren",
    ]
 
    # output_format options:
    #   "txt"           → plain-text file
    #   "csv"           → tab-separated CSV
    #   "markdown"      → Markdown document
    #   "json"          → JSON array
    #   "anki"          → both Anki decks (full vocab + article drill)
    #   "anki_full"     → Anki full-vocab deck only
    #   "anki_articles" → Anki article-drill deck only (nouns)
    scrape_dictionary(words, output_format="txt")

# %%

