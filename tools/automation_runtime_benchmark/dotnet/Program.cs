using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Playwright;

const string PlaywrightVersion = "1.56.0";

var options = ParseArgs(args);
var runtime = Required(options, "runtime");
var fixturePath = Path.GetFullPath(options.GetValueOrDefault("fixture", "../fixture.html"));
var outputPath = Path.GetFullPath(Required(options, "output"));
var rounds = int.Parse(options.GetValueOrDefault("rounds", "5"));
if (rounds < 1) throw new ArgumentException("--rounds must be positive");
if (!File.Exists(fixturePath)) throw new FileNotFoundException("fixture not found", fixturePath);

var fixture = await File.ReadAllTextAsync(fixturePath);
var outputDirectory = Path.Combine(Path.GetTempPath(), "automation-runtime-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(outputDirectory);
var audioPath = Path.Combine(outputDirectory, "benchmark-audio.mp3");
await File.WriteAllBytesAsync(audioPath, "ID3\x04\x00runtime-benchmark"u8.ToArray());

var records = new List<Dictionary<string, object>>();
var launchTimer = Stopwatch.StartNew();
using var playwright = await Playwright.CreateAsync();
await using var browser = await playwright.Chromium.LaunchAsync(new BrowserTypeLaunchOptions
{
    Headless = true,
    Args = new[] { "--disable-background-networking", "--no-first-run", "--disable-dev-shm-usage" },
});
var launchMs = launchTimer.Elapsed.TotalMilliseconds;

try
{
    for (var round = 1; round <= rounds; round++)
    {
        var page = await browser.NewPageAsync(new BrowserNewPageOptions
        {
            ViewportSize = new ViewportSize { Width = 1280, Height = 720 },
        });
        try
        {
            records.Add(await RunRoundAsync(page, fixture, audioPath, outputDirectory, round));
        }
        finally
        {
            await page.CloseAsync();
        }
    }
}
finally
{
    Directory.Delete(outputDirectory, recursive: true);
}

var result = new Dictionary<string, object>
{
    ["runtime"] = runtime,
    ["host_platform"] = RuntimeInformation.OSDescription,
    ["dotnet"] = Environment.Version.ToString(),
    ["playwright"] = PlaywrightVersion,
    ["rounds"] = rounds,
    ["launch_ms"] = Math.Round(launchMs, 2),
    ["records"] = records,
    ["median_ms"] = new Dictionary<string, double>
    {
        ["base_ms"] = Median(records, "base_ms"),
        ["content_ms"] = Median(records, "content_ms"),
        ["audio_ms"] = Median(records, "audio_ms"),
        ["total_ms"] = Median(records, "total_ms"),
    },
};
Directory.CreateDirectory(Path.GetDirectoryName(outputPath)!);
await File.WriteAllTextAsync(outputPath, JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }) + Environment.NewLine);
Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));

static async Task<Dictionary<string, object>> RunRoundAsync(
    IPage page,
    string fixture,
    string audioPath,
    string outputDirectory,
    int round)
{
    await page.SetContentAsync(fixture);
    var totalTimer = Stopwatch.StartNew();

    var timer = Stopwatch.StartNew();
    await page.Locator("[data-testid=paper-title]").FillAsync($"benchmark-{round}");
    await page.Locator("[data-testid=paper-year]").FillAsync("2026");
    await page.Locator("[data-testid=paper-duration]").FillAsync("45");
    var selects = new[]
    {
        ("category", "听说考试"), ("province", "广东省"), ("city", "广州市"),
        ("district", "天河区"), ("stage", "初中"), ("grade", "九年级"),
        ("kind", "中考模拟"),
    };
    foreach (var (key, value) in selects)
    {
        var field = page.Locator($"[data-testid=select-{key}]");
        await field.Locator(".select-trigger").ClickAsync();
        var option = field.Locator(".option").Filter(new LocatorFilterOptions { HasText = value }).Last;
        await option.WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible });
        await option.ClickAsync();
        await field.Locator(".select-trigger").Filter(new LocatorFilterOptions { HasText = value })
            .WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible });
    }
    var multi = page.Locator("[data-testid=select-districts]");
    foreach (var value in new[] { "天河区", "越秀区", "海珠区" })
    {
        await multi.Locator(".select-trigger").ClickAsync();
        var option = multi.Locator(".option").Filter(new LocatorFilterOptions { HasText = value }).Last;
        await option.WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible });
        await option.ClickAsync();
    }
    await multi.Locator(".select-trigger").Filter(new LocatorFilterOptions { HasText = "海珠区" })
        .WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible });
    await page.Locator("[data-testid=template-choice]").CheckAsync();
    await page.Locator("[data-testid=next]").ClickAsync();
    await page.Locator("#content-stage").WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible });
    var baseMs = timer.Elapsed.TotalMilliseconds;

    timer.Restart();
    for (var sectionIndex = 0; sectionIndex < 6; sectionIndex++)
    {
        await page.Locator($".outline[data-section='{sectionIndex}']").ClickAsync();
        var section = page.Locator($".content-section[data-section='{sectionIndex}']");
        await section.WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible });
        var cards = section.Locator(".question-card");
        if (await cards.CountAsync() != 3) throw new InvalidOperationException($"section {sectionIndex} card count mismatch");
        for (var cardIndex = 0; cardIndex < 3; cardIndex++)
        {
            var card = cards.Nth(cardIndex);
            await card.Locator(".editor[data-testid=prompt]").FillAsync($"section {sectionIndex} prompt {cardIndex}");
            await card.Locator(".editor[data-testid=translation]").FillAsync($"section {sectionIndex} translation {cardIndex}");
            await card.Locator("[data-testid=score]").FillAsync("1");
            await card.Locator("[data-testid=duration]").FillAsync("5");
            await card.Locator("[data-testid=audio]").SetInputFilesAsync(audioPath);
            await card.Locator(".audio-name").Filter(new LocatorFilterOptions { HasText = Path.GetFileName(audioPath) })
                .WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible });
            await card.Locator(".answer[data-answer=B]").ClickAsync();
        }
    }
    await page.Locator("[data-testid=save]").ClickAsync();
    await page.Locator("[data-testid=save-status]").Filter(new LocatorFilterOptions { HasText = "保存成功" })
        .WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible });
    var contentMs = timer.Elapsed.TotalMilliseconds;

    timer.Restart();
    await page.Locator("[data-testid=generate]").ClickAsync();
    var works = page.Locator("#works");
    await works.WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible });
    var rows = works.Locator("input[type=checkbox]");
    foreach (var index in new[] { 0, 2, 4 }) await rows.Nth(index).CheckAsync();
    await page.WaitForFunctionAsync("selector => !document.querySelector(selector).disabled", "[data-testid=download]");
    var downloads = new List<IDownload>();
    page.Download += (_, download) => { lock (downloads) downloads.Add(download); };
    await page.Locator("[data-testid=download]").ClickAsync();
    await page.Locator("#download-modal").WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible });
    await page.Locator("[data-testid=confirm-download]").ClickAsync();
    var deadline = DateTime.UtcNow.AddSeconds(10);
    while (true)
    {
        lock (downloads)
        {
            if (downloads.Count >= 3) break;
        }
        if (DateTime.UtcNow >= deadline) throw new TimeoutException($"expected 3 downloads, got {downloads.Count}");
        await page.WaitForTimeoutAsync(25);
    }
    lock (downloads)
    {
        foreach (var download in downloads) download.SaveAsAsync(Path.Combine(outputDirectory, download.SuggestedFilename)).GetAwaiter().GetResult();
    }
    var audioMs = timer.Elapsed.TotalMilliseconds;

    return new Dictionary<string, object>
    {
        ["round"] = round,
        ["base_ms"] = Math.Round(baseMs, 2),
        ["content_ms"] = Math.Round(contentMs, 2),
        ["audio_ms"] = Math.Round(audioMs, 2),
        ["total_ms"] = Math.Round(totalTimer.Elapsed.TotalMilliseconds, 2),
    };
}

static double Median(List<Dictionary<string, object>> records, string key)
{
    var values = records.Select(record => Convert.ToDouble(record[key])).OrderBy(value => value).ToArray();
    var middle = values.Length / 2;
    return Math.Round(values.Length % 2 == 1 ? values[middle] : (values[middle - 1] + values[middle]) / 2, 2);
}

static Dictionary<string, string> ParseArgs(string[] args)
{
    var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
    for (var index = 0; index < args.Length; index++)
    {
        if (!args[index].StartsWith("--", StringComparison.Ordinal)) continue;
        var key = args[index][2..];
        var value = index + 1 < args.Length ? args[++index] : "";
        result[key] = value;
    }
    return result;
}

static string Required(Dictionary<string, string> options, string key)
    => options.TryGetValue(key, out var value) && !string.IsNullOrWhiteSpace(value)
        ? value
        : throw new ArgumentException($"missing --{key}");
