/* core/computer_use/stealth_browser.js
   WW Computer Use — Browser Stealth Control Layer
   Uses puppeteer-extra + stealth to connect CDP, bypassing anti-bot detection. */

const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
puppeteer.use(StealthPlugin());

async function main() {
    const args = JSON.parse(process.argv[2]);
    const { action, params, cdp_url } = args;

    try {
        // Connect to existing browser via CDP
        const browser = await puppeteer.connect({
            browserURL: cdp_url || 'http://127.0.0.1:9222',
            defaultViewport: null,
        });

        const pages = await browser.pages();
        const page = pages[0] || await browser.newPage();

        let result = { success: true };

        switch (action) {
            case 'navigate': {
                await page.goto(params.url, { waitUntil: 'networkidle0', timeout: 30000 });
                break;
            }
            case 'screenshot': {
                const buf = await page.screenshot({ type: 'png', fullPage: false });
                const path = `C:\\Users\\Public\\playwright\\cdp_shot_${Date.now()}.png`;
                require('fs').writeFileSync(path, buf);
                result.path = '/mnt/c/Users/Public/playwright/' + path.split('\\').pop();
                break;
            }
            case 'text': {
                result.text = await page.evaluate(() => document.body.innerText);
                break;
            }
            case 'title': {
                result.title = await page.title();
                break;
            }
            case 'click': {
                await page.click(params.selector, { timeout: 10000 });
                break;
            }
            case 'fill': {
                await page.type(params.selector, params.text, { delay: 10 });
                break;
            }
            case 'evaluate': {
                result.value = await page.evaluate(new Function(params.code));
                break;
            }
            case 'get_element': {
                result.found = await page.$(params.selector) !== null;
                if (result.found && params.get_text) {
                    result.text = await page.$eval(params.selector, el => el.textContent);
                }
                break;
            }
            default:
                result = { success: false, error: `Unknown action: ${action}` };
        }

        console.log(JSON.stringify(result));
        await browser.disconnect();
        process.exit(0);
    } catch (e) {
        console.log(JSON.stringify({ success: false, error: e.message }));
        process.exit(1);
    }
}

main();
