#!/usr/bin/env python3
"""Generate the locked English prompt set for 788 voice-conversion training.

The corpus is intentionally deterministic.  Training, validation, and test
sentences have separate IDs so validation/test audio can be kept out of model
training and used for honest held-out evaluation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path("datasets/788/prompts")
CORPUS_VERSION = "1.0"


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    split: str
    category: str
    text: str


def _lower_initial(value: str) -> str:
    return value[:1].lower() + value[1:]


def _training_texts() -> list[tuple[str, str]]:
    prompts: list[tuple[str, str]] = []

    neutral_clauses = [
        ("The careful engineer", "checks", "the safety checklist"),
        ("A curious student", "records", "the laboratory measurement"),
        ("The neighborhood baker", "places", "a basket of fresh bread"),
        ("An experienced pilot", "reviews", "the weather report"),
        ("The patient librarian", "organizes", "a collection of old photographs"),
        ("A friendly mechanic", "repairs", "a portable radio"),
        ("The morning reporter", "records", "the afternoon announcement"),
        ("A talented musician", "reviews", "the printed sheet music"),
        ("The project manager", "updates", "the final schedule"),
        ("An observant traveler", "follows", "a detailed map"),
        ("The local gardener", "carries", "a tray of young plants"),
        ("A confident teacher", "explains", "a set of clear instructions"),
        ("The medical researcher", "labels", "the laboratory sample"),
        ("A thoughtful designer", "measures", "a small wooden model"),
        ("The museum guide", "reviews", "the visitor request"),
        ("A reliable courier", "delivers", "the blue package"),
        ("The workshop technician", "compares", "two polished silver tools"),
        ("A careful archivist", "opens", "the revised document"),
        ("The event volunteer", "moves", "a fragile glass container"),
        ("A safety inspector", "checks", "the delivery receipt"),
        ("The restaurant chef", "selects", "a basket of fresh fruit"),
        ("A skilled carpenter", "measures", "the narrow wooden panel"),
        ("The clinic nurse", "records", "the latest patient notes"),
        ("A local shopkeeper", "updates", "the weekly inventory"),
        ("The studio photographer", "carries", "the camera equipment"),
    ]
    locations = [
        "beside the quiet station",
        "inside the main office",
        "near the eastern entrance",
        "across the narrow bridge",
        "under the reading lamp",
        "behind the community hall",
        "at the riverside market",
        "outside the central library",
        "along the garden path",
        "in the second-floor studio",
        "next to the green warehouse",
        "by the old stone fountain",
        "within the testing room",
    ]
    endings = [
        "before sunrise",
        "after the lunch break",
        "during the weekly inspection",
        "without making unnecessary noise",
        "while the room is still empty",
        "before the next visitor arrives",
        "on a cool autumn morning",
        "as the evening traffic begins",
        "with calm and steady attention",
        "before the final bell rings",
        "when the weather becomes clear",
    ]
    for index in range(100):
        subject, action, obj = neutral_clauses[index % len(neutral_clauses)]
        location = locations[(index * 7) % len(locations)]
        ending = endings[(index * 9) % len(endings)]
        variant = index % 4
        if variant == 0:
            text = f"{subject} {action} {obj} {location} {ending}."
        elif variant == 1:
            text = (
                f"{ending.capitalize()}, {_lower_initial(subject)} {action} "
                f"{obj} {location}."
            )
        elif variant == 2:
            text = (
                f"{location.capitalize()}, "
                f"{_lower_initial(subject)} quietly {action} {obj} {ending}."
            )
        else:
            text = (
                f"{subject} {action} {obj}; the work continues {location} "
                f"{ending}."
            )
        prompts.append(("neutral", text))

    requests = [
        "confirm the reservation",
        "send the updated address",
        "close the side window",
        "repeat the last instruction",
        "check the departure platform",
        "save a copy of the receipt",
        "bring two clean glasses",
        "call the service desk",
        "mark the correct entrance",
        "turn the volume down",
        "review the attached note",
        "hold the elevator door",
        "spell the customer name",
        "move the meeting to Friday",
        "explain the return policy",
    ]
    situations = [
        "the connection may drop again",
        "the guests are already waiting",
        "the original message was unclear",
        "we need an accurate record",
        "the building closes early today",
        "the road ahead is under repair",
        "the package is unusually fragile",
        "the schedule changed this morning",
        "the room is getting too warm",
        "the next train is nearly full",
        "the manager needs an answer",
        "the weather could become worse",
        "the payment has not appeared yet",
    ]
    replies = [
        "I can take care of that now",
        "I'll check and call you back",
        "that should only take a minute",
        "we can solve it together",
        "I've written down the details",
        "let me verify the information first",
        "the request has been received",
        "I understand what you need",
        "we don't need to rush",
        "I'll make the change immediately",
        "the correct option is highlighted",
    ]
    for index in range(60):
        request = requests[index % len(requests)]
        situation = situations[(index * 5) % len(situations)]
        reply = replies[(index * 7) % len(replies)]
        variant = index % 4
        if variant == 0:
            text = f"Could you {request}, please? {situation.capitalize()}."
        elif variant == 1:
            text = f"Please {request}; {situation}. {reply.capitalize()}."
        elif variant == 2:
            text = f"Would you {request} before we continue? {reply.capitalize()}."
        else:
            text = f"Since {situation}, please {request}. {reply.capitalize()}."
        prompts.append(("conversation", text))

    question_topics = [
        "the earliest bus to the airport",
        "the warranty on this camera",
        "our appointment tomorrow morning",
        "the safest route through the park",
        "the reason for the delayed payment",
        "the nearest open pharmacy",
        "the final score from last night",
        "the password reset procedure",
        "the new recycling schedule",
        "the size of the conference room",
        "the return date for this book",
        "the cause of the power interruption",
        "the menu for tonight's dinner",
        "the status of the repair order",
        "the correct pronunciation of this name",
        "the expected delivery window",
        "the location of the emergency exit",
        "the price of a weekend ticket",
        "the results of the latest test",
        "the difference between these two plans",
    ]
    question_contexts = [
        "I need to make a decision soon",
        "the website gives two different answers",
        "we are leaving in less than an hour",
        "my earlier note may have been incorrect",
        "the printed notice is difficult to read",
        "a clear explanation would help everyone",
        "the customer is waiting on the line",
        "we should confirm it before proceeding",
        "the old information is no longer reliable",
        "I want to avoid another mistake",
        "the team has not received an update",
        "this detail affects the entire schedule",
        "the answer was omitted from the report",
    ]
    for index in range(40):
        topic = question_topics[index % len(question_topics)]
        context = question_contexts[(index * 3) % len(question_contexts)]
        if index % 4 == 0:
            text = f"What can you tell me about {topic}? {context.capitalize()}."
        elif index % 4 == 1:
            text = f"Do you know anything about {topic}? {context.capitalize()}."
        elif index % 4 == 2:
            text = f"Could we verify {topic} before noon? {context.capitalize()}."
        else:
            text = f"Why has nobody confirmed {topic} yet? {context.capitalize()}."
        prompts.append(("questions", text))

    months = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    streets = [
        "Oak Street",
        "River Road",
        "Pine Avenue",
        "Market Lane",
        "Cedar Drive",
        "Willow Court",
        "King Street",
        "Harbor Way",
        "Sunset Boulevard",
        "Lake View Road",
    ]
    ordinal_words = ["first", "second", "third", "fourth", "fifth"]
    for index in range(60):
        variant = index % 12
        cycle = index // 12
        n = index + 1
        if variant == 0:
            text = (
                f"Invoice {4100 + n} totals ${87 + n}.{35 + cycle * 12:02d}, "
                f"including {5 + cycle}% tax and a service fee of "
                f"${3 + cycle}.{20 + cycle * 9:02d}."
            )
        elif variant == 1:
            text = (
                f"Flight AX{210 + n} leaves at {6 + cycle}:{10 + cycle * 9:02d} "
                f"p.m. on {months[n % 12]} {8 + cycle * 3}, 2027."
            )
        elif variant == 2:
            text = (
                f"Call 202-555-{100 + cycle:04d}, then enter extension "
                f"{240 + cycle * 17} after the tone."
            )
        elif variant == 3:
            text = (
                f"The container is {24 + cycle * 3}.{2 + cycle} centimeters wide, "
                f"{15 + cycle * 2} centimeters tall, and weighs "
                f"{3 + cycle}.{4 + cycle} kilograms."
            )
        elif variant == 4:
            text = (
                f"Software version {2 + cycle}.{cycle + 1}.{14 + cycle} reduced "
                f"the average delay from {360 + cycle * 23} to "
                f"{180 + cycle * 11} milliseconds."
            )
        elif variant == 5:
            text = (
                f"Deliver order {78000 + n} to {100 + n} {streets[n % len(streets)]} "
                f"between {7 + cycle}:15 a.m. and {1 + cycle}:45 p.m."
            )
        elif variant == 6:
            text = (
                f"At 6:{20 + cycle * 7:02d} a.m., the temperature was minus "
                f"{3 + cycle}.5 degrees Celsius, then rose by {5 + cycle} degrees."
            )
        elif variant == 7:
            text = (
                f"Mix {cycle + 1} and one-half cups of water with "
                f"three-quarters of a cup of dry grain."
            )
        elif variant == 8:
            text = (
                f"The {ordinal_words[cycle]} runner finished stage {12 + cycle} "
                f"in {38 + cycle} minutes and {14 + cycle * 3} seconds."
            )
        elif variant == 9:
            text = (
                f"Send case {9200 + n} to support{cycle + 1}@example.com, "
                f"then visit https://example.com/help/{cycle + 1}."
            )
        elif variant == 10:
            text = (
                f"The survey included {1250 + cycle * 375:,} responses, and "
                f"{72 + cycle * 3}.{cycle + 1}% selected the first option."
            )
        else:
            text = (
                f"Train R{70 + cycle} traveled at {58 + cycle * 7}.5 miles per "
                f"hour for {12 + cycle * 4} minutes before slowing down."
            )
        prompts.append(("numbers", text))

    technical_cases = [
        (
            "The backup service stores encrypted records across three regions",
            [
                "This keeps the records available during maintenance",
                "Encrypted replicas remain available if one region fails",
                "The operator can inspect every access event later",
                "No personal information is stored in plain text",
            ],
        ),
        (
            "A compact neural network detects small changes in real time",
            [
                "The operator can inspect every detected change later",
                "The design also reduces unnecessary processing",
                "The new method is easier to test and reproduce",
                "It flags values outside the expected range",
            ],
        ),
        (
            "The optical sensor compares each measurement with a trusted baseline",
            [
                "As a result, fewer errors reach the final report",
                "The calibration method is easy to test and reproduce",
                "Each measurement includes a precise timestamp",
                "It rejects readings outside the safe range",
            ],
        ),
        (
            "Our database cluster balances incoming work between available servers",
            [
                "This keeps the response fast and predictable",
                "The cluster remains available during maintenance",
                "This approach works even when bandwidth is limited",
                "The operator can rebalance traffic manually at any time",
            ],
        ),
        (
            "The navigation system restarts automatically after a brief interruption",
            [
                "The system remains available during maintenance",
                "A manual override remains available at all times",
                "The operator can inspect every restart event later",
                "Each restart event includes a precise timestamp",
            ],
        ),
        (
            "A secure authentication token expires after a fixed period",
            [
                "The authentication service rejects it after that point",
                "The service checks the token before granting access",
                "This expiration policy is easy to test and reproduce",
                "Each expiration event includes a precise timestamp",
            ],
        ),
        (
            "The audio processing pipeline filters unwanted noise from the signal",
            [
                "This makes quiet speech easier to understand",
                "As a result, fewer artifacts reach the final recording",
                "The processing remains stable during long-running tasks",
                "This approach works even when bandwidth is limited",
            ],
        ),
        (
            "This solar power controller maintains a stable output under heavy load",
            [
                "A manual override remains available at all times",
                "This behavior is important for long-running tasks",
                "The system remains stable during scheduled maintenance",
                "The controller rejects voltages outside the safe range",
            ],
        ),
        (
            "The climate monitoring station reports unusual activity to the control center",
            [
                "The operator can inspect every alert later",
                "The control center rejects measurements outside the safe range",
                "Each warning includes a precise timestamp",
                "The monitoring method is easier to test and reproduce",
            ],
        ),
        (
            "A distributed message queue delivers tasks to available workers",
            [
                "Each task includes a precise timestamp",
                "This approach works even when bandwidth is limited",
                "As a result, fewer tasks miss their deadline",
                "This keeps the response fast and predictable",
            ],
        ),
    ]
    abbreviation_texts = [
        "The API returns a JSON response after validating the access token.",
        "A modern CPU can process several audio frames in parallel.",
        "Please open the URL and verify that the HTTPS certificate is valid.",
        "The GPS receiver lost its signal inside the underground garage.",
        "NASA published a PDF summary of the latest satellite mission.",
        "Connect the USB-C cable before starting the firmware update.",
        "The HTML page loads its CSS file from a separate server.",
        "Our AI model sends anonymized metrics to the monitoring dashboard.",
        "The FAQ explains how to reset a PIN without contacting support.",
        "An SSD usually opens large project files faster than a hard drive.",
    ]
    for index in range(50):
        if index >= 40:
            text = abbreviation_texts[index - 40]
        else:
            clause, compatible_results = technical_cases[index % len(technical_cases)]
            result = compatible_results[index // len(technical_cases)]
            text = f"{clause}. {result}."
        prompts.append(("technical", text))

    phonetic_texts = [
        "Bright copper kettles clicked softly beside the thick wooden shelf.",
        "Victor packed five velvet jackets in a wide gray box.",
        "The cheerful judge chose a fresh peach and a jar of ginger.",
        "Quick waves washed shells and smooth stones across the shore.",
        "Zoe carefully zipped the beige travel pouch before breakfast.",
        "Three thin threads were woven through the leather strap.",
        "The young singer breathed slowly before reaching the highest note.",
        "Fresh coffee and warm waffles filled the kitchen with a sweet smell.",
        "George gently adjusted the fragile hinge with a narrow wrench.",
        "A noisy squirrel chased two blue jays through the orchard.",
        "The chef sliced crisp vegetables while the broth quietly simmered.",
        "Purple flowers grew between the rough bricks of the garden wall.",
        "William whispered that the weather would improve by Thursday.",
        "The silver train curved sharply around the frozen hillside.",
        "A tiny moth fluttered beneath the bright porch light.",
        "Grace brought fresh bread, strong cheese, and green grapes.",
        "The bronze bell rang twice across the crowded village square.",
        "Philip found a smooth shell near the edge of the marsh.",
        "The red truck splashed through a shallow patch of muddy water.",
        "Six brave hikers crossed the narrow wooden bridge at dawn.",
        "A black cat stretched lazily beside the warm kitchen stove.",
        "The quiet child drew a giant whale with purple chalk.",
        "Sharp winter winds shook the branches above the church.",
        "The friendly dentist explained why gentle brushing matters.",
        "A striped zebra watched the curious fox from across the field.",
        "The drummer struck a steady rhythm with both wooden sticks.",
        "Fresh snow covered every roof, fence, and parked bicycle.",
        "The small boat drifted beyond the bright green buoy.",
        "Rachel wrapped the fragile vase in several thick blankets.",
        "The brown dog jumped quickly over a fallen branch.",
        "Soft shadows moved across the wall as the candle flickered.",
        "The brave firefighter checked each hose before the drill.",
        "A cheerful crowd cheered when the final whistle sounded.",
        "The old clock chimed exactly as the theater doors opened.",
        "Heavy rain drummed against the glass throughout the night.",
        "The ship passed the sheep grazing near the deep blue sea.",
        "Luke pulled a full blue book from the wooden shelf.",
        "Dan set the red bag beside the black desk.",
        "We live near the green field, but we leave before evening.",
        "A rare bird circled near the old pier before disappearing.",
    ]
    prompts.extend(("phonetic", text) for text in phonetic_texts)

    expressive_scenes = [
        "the missing keys were in my coat pocket",
        "our team finished the project two days early",
        "we caught the last ferry just before it left the harbor",
        "a rainbow appeared above the dark clouds",
        "the repaired radio suddenly began to play",
        "the surprise package arrived this morning",
        "the trail was closed by a fallen tree",
        "everyone remembered the secret celebration",
        "the tiny plant produced its first flower",
        "the ticket was still valid after all",
        "the lights returned just before dinner",
        "the dog had carried the newspaper indoors",
        "the distant rumble was only a passing truck",
        "the old photograph showed the same house",
        "the concert ended with a beautiful quiet chord",
    ]
    expressive_reactions = [
        "What an unexpected relief",
        "I honestly could not believe it",
        "That was the best possible outcome",
        "For a moment, nobody knew what to say",
        "It was hard to believe we had missed something so obvious",
        "We laughed about it for the rest of the day",
        "I felt both surprised and grateful",
        "That news completely changed our plans",
        "Everyone stopped and listened carefully",
        "It seemed disappointing at first",
        "What a wonderful way to end the evening",
        "I wish we had discovered it sooner",
        "The whole room became suddenly quiet",
    ]
    prosody_texts = [
        "You finished the report.",
        "You finished the report?",
        "You finished the report!",
        "The package arrived this morning.",
        "The package arrived this morning?",
        "The package arrived this morning!",
    ]
    prompts.extend(("expressive", text) for text in prosody_texts)
    for index in range(24):
        scene = expressive_scenes[index % len(expressive_scenes)]
        reaction = expressive_reactions[(index * 5) % len(expressive_reactions)]
        if index % 3 == 0:
            text = f"{reaction}! It turned out that {scene}."
        elif index % 3 == 1:
            text = f"{reaction}. Apparently, {scene}!"
        else:
            text = f"Did you hear that {scene}? {reaction}."
        prompts.append(("expressive", text))

    short_status_texts = [
        "Everything is ready now.",
        "Please try that step again.",
        "The connection has been lost.",
        "Your audio file is ready.",
        "Please check the delivery address.",
        "The request was canceled.",
        "Recording will begin shortly.",
        "The update finished successfully.",
        "No matching result was found.",
        "Please wait for the signal.",
        "The microphone is muted.",
        "Your session has expired.",
        "The download is almost complete.",
        "Please enter a valid number.",
        "The device is connected.",
        "Something went wrong.",
        "The message was sent.",
        "Please choose another option.",
        "The service is available again.",
        "You may continue now.",
    ]
    prompts.extend(("short_status", text) for text in short_status_texts)

    if len(prompts) != 400:
        raise AssertionError(f"expected 400 training prompts, got {len(prompts)}")
    return prompts


VALIDATION_TEXTS = [
    "The morning train arrived quietly while a light rain covered the platform before the station clock struck nine.",
    "Could you verify the account number before sending the final confirmation and double-check the recipient's email address?",
    "A narrow beam of sunlight crossed the floor and reached the blue cabinet near the entrance to the reading room.",
    "The repair should cost $146.75 and take approximately three business days, including the time needed to obtain replacement materials.",
    "Why did the navigation system choose the longer route through the valley instead of following the shorter highway?",
    "Please leave the signed document with the receptionist on the third floor before the afternoon courier leaves the building.",
    "The young actor paused, took a breath, and delivered the final line calmly.",
    "Our appointment is at 9:35 a.m. on October 18, 2027.",
    "Fresh basil, roasted garlic, and lemon gave the soup a bright flavor.",
    "The API returned an HTTP 503 error, but the retry completed successfully.",
    "What a strange coincidence! We selected exactly the same seat again.",
    "A gentle breeze moved the white curtains beside the open window.",
    "The package weighs 7.3 kilograms and measures 42 centimeters in length.",
    "Would you read the street name aloud so I can enter it correctly?",
    "The photographer waited until the clouds revealed the mountain peak.",
    "Version 4.8.16 completed the task in 219 milliseconds.",
    "Several bright fish moved beneath the surface of the shallow pond.",
    "Please compare the printed label with the number shown on the screen.",
    "The audience remained silent for a moment, then applauded with enthusiasm.",
    "How much time do we have before the northern gate closes?",
    "A compact filter removes background hum without damaging quiet consonants.",
    "The final payment of $83.20 is due on February 6.",
    "Rachel gently pushed the heavy drawer until the brass latch clicked.",
    "Could the weather delay tomorrow evening's outdoor performance?",
    "The museum displays a fragile compass beside several handwritten journals.",
    "Call 202-555-0166 and ask for extension 304.",
    "The tired traveler smiled when the familiar skyline appeared.",
    "Please keep every original recording, even if a converted copy sounds cleaner.",
    "Please open https://example.com/help and choose the audio settings page.",
    "What a beautiful surprise to find fresh flowers waiting at the door!",
    "The chef checked the sauce, lowered the heat, and covered the pan.",
    "Our backup completed at 11:42 p.m. without reporting a single error.",
    "A cheerful whistle echoed between the brick walls of the narrow alley.",
    "Can you explain why this result differs from yesterday's measurement?",
    "The green bicycle was parked beneath a flowering cherry tree.",
    "Order 73184 will arrive between 2:15 and 4:45 on Wednesday.",
    "The small sensor remained stable despite vibration, heat, and dust.",
    "The verification code is no longer valid, so please request a new one.",
    "Although the storm delayed the outdoor ceremony, the technicians protected every instrument and reopened the stage before the audience arrived.",
    "Did anyone notice the silver key beside the folded newspaper?",
]


TEST_TEXTS = [
    "The courier placed a sealed envelope beside the old brass telephone.",
    "Why was the afternoon flight moved from gate 12 to gate 27?",
    "Please confirm that the replacement part matches the original drawing.",
    "The total comes to $294.60 after an 8% discount.",
    "A sudden gust scattered yellow leaves across the empty basketball court.",
    "Could you repeat the access code slowly, one digit at a time?",
    "The CPU usage dropped after the background indexing task completed.",
    "Our next review begins at 10:20 a.m. on March 14, 2028.",
    "The violin sounded warm and clear inside the small wooden room.",
    "What an incredible view! The entire coastline is visible from here.",
    "A careful technician measured the voltage before replacing the fuse.",
    "Call 212-555-0172 if the delivery has not arrived by Friday.",
    "The children watched three bright balloons drift above the rooftops.",
    "Would a later appointment make the journey easier for you?",
    "The final report contains 36 charts, 12 tables, and 4 appendices.",
    "Soft thunder rolled beyond the hills as the evening sky darkened.",
    "Your session will end in two minutes.",
    "The new encoder preserves quiet speech while reducing steady background noise.",
    "How did the blue suitcase end up behind the locked office door?",
    "A friendly neighbor brought warm bread and a jar of blackberry jam.",
    "The motor reached 2,450 revolutions per minute without overheating.",
    "Could we postpone the announcement until every participant has arrived?",
    "The narrow path curved through tall grass toward a quiet lake.",
    "Version 7.2.9 reduced memory use by 17.5 percent.",
    "What a relief! The missing document was attached to the earlier message.",
    "The glass sculpture reflected green, violet, and golden light.",
    "Please read invoice 80419 before approving the electronic payment.",
    "The API gateway recovered within 320 milliseconds of the outage.",
    "Why does this wooden box feel heavier than the metal one?",
    "A small crowd gathered as the street musician played the final melody.",
    "The reservation covers four guests from June 11 through June 14.",
    "Could you describe the difference between the rough and smooth samples?",
    "The patient guide explained each step in a steady, reassuring voice.",
    "Visit https://example.org/status and enter reference 67231.",
    "A bright flash was followed by a low rumble several seconds later.",
    "Please restart the router only after the green status light stops blinking.",
    "The laboratory recorded a pressure of 101.6 kilopascals.",
    "How wonderful to hear everyone laughing together again!",
    "After reviewing every measurement, the engineering team wrote a detailed summary and recommended three practical changes for the next production run.",
    "Did the quiet buzzing sound begin before or after the update?",
]


def build_corpus() -> list[Prompt]:
    prompts: list[Prompt] = []
    for index, (category, text) in enumerate(_training_texts(), start=1):
        prompts.append(Prompt(f"tr_{index:04d}", "train", category, text))
    for index, text in enumerate(VALIDATION_TEXTS, start=1):
        prompts.append(Prompt(f"va_{index:04d}", "validation", "held_out", text))
    for index, text in enumerate(TEST_TEXTS, start=1):
        prompts.append(Prompt(f"te_{index:04d}", "test", "held_out", text))

    if len(VALIDATION_TEXTS) != 40 or len(TEST_TEXTS) != 40:
        raise AssertionError("validation and test splits must each contain 40 prompts")
    ids = [prompt.prompt_id for prompt in prompts]
    texts = [prompt.text.casefold() for prompt in prompts]
    if len(ids) != len(set(ids)):
        raise AssertionError("duplicate prompt ID")
    if len(texts) != len(set(texts)):
        raise AssertionError("duplicate prompt text")
    for prompt in prompts:
        if not (prompt.text[0].isupper() or prompt.text[0].isdigit()):
            raise AssertionError(f"{prompt.prompt_id} does not start with a capital")
        for match in re.finditer(r"[.!?]\s+([A-Za-z])", prompt.text):
            prefix = prompt.text[max(0, match.start() - 3) : match.start() + 1]
            if prefix.casefold().endswith(("a.m.", "p.m.")):
                continue
            if not match.group(1).isupper():
                raise AssertionError(
                    f"{prompt.prompt_id} starts a sentence with lowercase text"
                )
    return prompts


def write_corpus(output_dir: Path, *, force: bool = False) -> tuple[Path, Path]:
    prompts = build_corpus()
    output_dir.mkdir(parents=True, exist_ok=True)
    tsv_path = output_dir / "788_corpus.tsv"
    meta_path = output_dir / "788_corpus.meta.json"
    if not force:
        existing = [path for path in (tsv_path, meta_path) if path.exists()]
        if existing:
            raise FileExistsError(
                "输出已存在；如需按当前脚本重建，请加 --force："
                + ", ".join(map(str, existing))
            )

    with tsv_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow(["id", "split", "category", "text"])
        for prompt in prompts:
            writer.writerow(
                [prompt.prompt_id, prompt.split, prompt.category, prompt.text]
            )

    digest = hashlib.sha256(tsv_path.read_bytes()).hexdigest()
    split_counts = Counter(prompt.split for prompt in prompts)
    category_counts = Counter(prompt.category for prompt in prompts)
    word_count = sum(len(prompt.text.split()) for prompt in prompts)
    metadata = {
        "version": CORPUS_VERSION,
        "prompt_file": tsv_path.name,
        "sha256": digest,
        "prompt_count": len(prompts),
        "split_counts": dict(sorted(split_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "word_count": word_count,
        "estimated_minutes_at_130_wpm": round(word_count / 130, 1),
        "estimated_minutes_at_160_wpm": round(word_count / 160, 1),
        "naming_rule": "<id>.<wav|flac|mp3|m4a|aac|ogg|opus>",
        "warning": (
            "Validation and test utterances are locked holdouts. "
            "Never copy them into the training split."
        ),
    }
    meta_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return tsv_path, meta_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 788 英语训练/验证/测试语料表")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--force", action="store_true", help="覆盖已有语料表")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        tsv_path, meta_path = write_corpus(args.output_dir, force=args.force)
    except FileExistsError as exc:
        raise SystemExit(str(exc)) from exc
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    print(f"已生成：{tsv_path}")
    print(f"元数据：{meta_path}")
    print(f"句数：{metadata['prompt_count']}")
    print(
        "预计时长："
        f"{metadata['estimated_minutes_at_160_wpm']}–"
        f"{metadata['estimated_minutes_at_130_wpm']} 分钟"
    )
    print(f"SHA-256：{metadata['sha256']}")


if __name__ == "__main__":
    main()
