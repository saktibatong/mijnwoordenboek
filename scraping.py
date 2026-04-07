#%%
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException
import time

# %%
def get_entry_data(driver, word, h2):
    """Extract data for a single h2.inline block. Returns None for missing fields."""

    # 1. TRANSLATION TITLE
    try:
        translation_title = h2.find_element(
            By.XPATH, "preceding-sibling::div[@class='aaa'][1]"
        ).text.strip()
    except NoSuchElementException:
        translation_title = None

    # 2. WORD FUNCTION
    word_function = h2.text.strip()  # h2 itself — safe, always present

    # 3. PART OF SPEECH
    part_of_speech = driver.execute_script("""
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
        inflections = driver.execute_script("""
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
    definitions = driver.execute_script("""
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
s
            // DEFINITION LINE — loose font/text nodes (e.g. "1) kind van ... - boy, kid")
            if (node.nodeType === 1 && node.tagName.toLowerCase() === "font") {
                let text = node.innerText.trim();
                if (text.match(/^\\d+\\)$/)) {
                    // Start a new definition entry when we see "1)", "2)", etc.
                    if (current) results.push(current);
                    current = { definition: text, examples: [] };
                } else if (current && text) {
                    current.definition += " " + text;
                }
            }

            // Text node between fonts (e.g. " - ")
            if (node.nodeType === 3 && current) {
                let text = node.textContent.trim();
                if (text) current.definition += " " + text;
            }

            // EXAMPLE TABLE — the indented sentence pairs
            if (node.nodeType === 1 && node.tagName.toLowerCase() === "table") {
                let text = node.innerText.trim();
                if (text.includes("Uitspraak") || text.includes("Verbuigingen")) {
                    node = node.nextSibling;
                    continue;
                }
                if (current && text) {
                    // Each row is one NL - EN example pair
                    node.querySelectorAll("tr").forEach(tr => {
                        let pair = tr.innerText.trim();
                        if (pair) current.examples.push(pair);
                    });
                }
            }

            node = node.nextSibling;
        }

        if (current) results.push(current);
        return results;
    """, h2)

    return {
        "Word": word,
        "Translation title": translation_title,
        "Word function": word_function,
        "Part of speech": part_of_speech,
        "Pronunciation": pronunciation,
        "Inflections": inflections,
        "Definitions": definitions,
    }

def get_word_data(driver, word):
    wait = WebDriverWait(driver, 10)

    # Input word
    search_box = wait.until(EC.presence_of_element_located((By.ID, "woord")))
    search_box.clear()
    search_box.send_keys(word)

    # Click "Vertaal"
    vertaal = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(),'Vertaal')]")))
    vertaal.click()

    # Wait for result title
    h2_list = wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "inline")))
    return [get_entry_data(driver, word, h2) for h2 in h2_list]

#%%
# MAIN
driver = webdriver.Edge()
driver.get("https://www.mijnwoordenboek.nl/")

wait = WebDriverWait(driver, 10)

# Set language NL → EN
Select(wait.until(EC.presence_of_element_located((By.ID, "src")))).select_by_value("NL")
Select(driver.find_element(By.ID, "des")).select_by_value("EN")

# Words to scrape
words = ["jongen"]
# words = ["jongen", "huis", "water"]

#%%
for word in words:
    data = get_word_data(driver, word)

    for item in data:
        for key, value in item.items():
            print(f"{key}: {value}")
    
    time.sleep(1)

driver.quit()

# %%

