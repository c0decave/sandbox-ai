#!/usr/bin/env python3
"""Codenames for starts: three words, ADJECTIVE-ANIMAL-THING, such as brisk-otter-lantern or calm-heron-tulip.

A start without --name gets one (unless "name_style": "project" in your defaults, or --name-style project, chose
the older AGENT-PROJECT-xxxx); `sandbox-ai rename NAME` without NEW gives a start a fresh one. The words are short
(3-8 letters), lower-case ASCII and harmless: no brands, no politics, no command or agent names of sandbox-ai, no
word made only of hex digits (a run id's look), no word of a security mode or a start's state. A codename is at most
8+1+8+1+8 = 26 characters, and 31 with the -xxxx a crowded machine adds after a few tries: well inside the 40 a
name may have. The words are chosen with `secrets`.

Standard library only.
"""
import secrets

ADJECTIVES = (
    "able", "agile", "airy", "amber", "amiable", "ample", "auburn", "azure", "balmy", "blissful", "bold", "bouncy",
    "brave", "breezy", "bright", "brisk", "bubbly", "calm", "candid", "careful", "cheery", "chilly", "chipper",
    "civil", "clean", "clear", "clever", "cloudy", "comfy", "cosmic", "cozy", "crimson", "crisp", "curly", "dainty",
    "dapper", "daring", "dashing", "deft", "dewy", "dreamy", "dusky", "dusty", "eager", "early", "earnest", "easy",
    "elated", "elegant", "elfin", "epic", "exact", "fabled", "fair", "fancy", "feisty", "festive", "fine", "firm",
    "fleet", "floral", "fluffy", "fond", "frank", "fresh", "frosty", "frugal", "funny", "fuzzy", "gallant",
    "genial", "gentle", "giddy", "gifted", "glad", "gleaming", "glossy", "glowing", "golden", "graceful", "grand",
    "grassy", "great", "hardy", "hazel", "hearty", "helpful", "honest", "humble", "icy", "ideal", "indigo",
    "ivory", "jaunty", "jazzy", "jolly", "jovial", "joyful", "keen", "khaki", "kind", "kindly", "lavish", "leafy",
    "lilac", "limber", "lively", "lofty", "loyal", "lucid", "lucky", "lunar", "lush", "magic", "majestic", "mauve",
    "mellow", "merry", "mighty", "mild", "minty", "misty", "modest", "mossy", "nifty", "nimble", "noble", "novel",
    "oaken", "olive", "patient", "peachy", "pearly", "peppy", "perky", "placid", "plucky", "plush", "polite",
    "proud", "quaint", "quick", "quiet", "quirky", "radiant", "rainy", "rapid", "rare", "regal", "rosy", "round",
    "royal", "ruddy", "rugged", "russet", "rustic", "sandy", "savvy", "scarlet", "sepia", "serene", "shiny",
    "silent", "silky", "silver", "simple", "sincere", "sleek", "smart", "smooth", "snappy", "snowy", "snug", "soft",
    "solar", "solid", "sparkly", "speedy", "spicy", "sporty", "spry", "starry", "steady", "stellar", "stout",
    "sturdy", "sublime", "sunlit", "sunny", "sweet", "tawny", "teal", "tender", "thrifty", "tidy", "tiny",
    "toasty", "tranquil", "tropical", "trusty", "twinkly", "upbeat", "upright", "urban", "useful", "valiant",
    "vast", "velvet", "verdant", "vernal", "vital", "vivid", "warm", "wavy", "whole", "wild", "windy", "wintry",
    "wiry", "wise", "witty", "wooden", "woolly", "young", "zany", "zealous", "zesty", "zippy",
)

ANIMALS = (
    "aardvark", "albacore", "alpaca", "anchovy", "anteater", "antelope", "auk", "avocet", "axolotl", "baboon",
    "badger", "beagle", "bear", "beaver", "bison", "bittern", "bluejay", "boar", "bobcat", "bonobo", "budgie",
    "buffalo", "bull", "bunny", "capybara", "caribou", "carp", "catfish", "cheetah", "chicken", "chipmunk",
    "cicada", "clam", "cockatoo", "cod", "condor", "cow", "coyote", "crab", "crane", "cricket", "crow", "cuckoo",
    "curlew", "deer", "dingo", "dodo", "dolphin", "donkey", "dormouse", "duck", "dugong", "eagle", "eel", "egret",
    "eland", "elephant", "elk", "emu", "ermine", "falcon", "ferret", "finch", "firefly", "fish", "flamingo",
    "flounder", "fox", "frog", "gazelle", "gerbil", "gibbon", "giraffe", "goat", "goldfish", "goose", "gopher",
    "gorilla", "grouse", "gull", "guppy", "hamster", "hare", "hawk", "hedgehog", "heron", "herring", "hippo",
    "horse", "ibex", "ibis", "iguana", "jackdaw", "jay", "kakapo", "kangaroo", "kestrel", "kiwi", "koala", "koi",
    "krill", "ladybug", "lamb", "lark", "lemming", "lemur", "leopard", "limpet", "lion", "llama", "lobster", "loon",
    "macaw", "magpie", "mallard", "manatee", "mandrill", "mantis", "marmot", "marten", "meerkat", "mink",
    "minnow", "mole", "mongoose", "monkey", "moose", "moth", "mouse", "mule", "narwhal", "newt", "nightjar",
    "ocelot", "octopus", "okapi", "opossum", "orca", "oriole", "osprey", "ostrich", "otter", "owl", "oyster",
    "panda", "panther", "parrot", "peacock", "pelican", "penguin", "petrel", "pheasant", "pigeon", "pika", "pike",
    "platypus", "plover", "pony", "poodle", "porpoise", "possum", "prawn", "puffin", "quail", "quokka", "rabbit",
    "raccoon", "raven", "reindeer", "rhino", "robin", "rooster", "salmon", "sardine", "seagull", "seahorse",
    "seal", "shark", "sheep", "shrew", "shrimp", "skink", "skylark", "sloth", "snail", "snipe", "sparrow",
    "squid", "squirrel", "starling", "stingray", "stoat", "stork", "sturgeon", "swallow", "swan", "tapir",
    "tarsier", "tern", "thrush", "tiger", "toad", "tortoise", "toucan", "trout", "tuna", "turtle", "urchin",
    "vicuna", "vole", "wallaby", "walrus", "warbler", "whale", "wildcat", "wolf", "wombat", "wren", "yak",
    "zebra", "zebu",
)

THINGS = (  # plants first, then what is found outdoors and at home
    "acorn", "alder", "almond", "aloe", "amaranth", "anemone", "apricot", "aster", "azalea", "bamboo", "banana",
    "barley", "basil", "bean", "beech", "begonia", "berry", "birch", "bluebell", "bonsai", "bramble", "briar",
    "bulrush", "cabbage", "cactus", "camellia", "carrot", "catnip", "cedar", "celery", "cherry", "chestnut",
    "chicory", "chive", "citrus", "clematis", "clover", "coconut", "comfrey", "cosmos", "cotton", "crocus",
    "cypress", "daffodil", "dahlia", "daisy", "elm", "fennel", "fern", "fig", "fir", "flax", "flower", "foxglove",
    "freesia", "gardenia", "garlic", "geranium", "ginger", "ginkgo", "gorse", "gourd", "grape", "hawthorn",
    "heather", "hibiscus", "holly", "hops", "hosta", "iris", "ivy", "jasmine", "juniper", "kale", "kelp",
    "kumquat", "larch", "laurel", "lavender", "leek", "lemon", "lentil", "lettuce", "lichen", "lily", "linden",
    "lupin", "magnolia", "mango", "maple", "marigold", "melon", "mimosa", "moss", "mulberry", "myrtle", "nettle",
    "nutmeg", "oak", "oat", "orchid", "oregano", "papaya", "parsley", "peach", "peanut", "pear", "pecan", "peony",
    "pepper", "petunia", "pine", "plum", "poppy", "potato", "primrose", "pumpkin", "quince", "radish", "reed",
    "rhubarb", "rose", "rosemary", "rowan", "saffron", "sequoia", "sorrel", "spruce", "squash", "sumac", "tansy",
    "thistle", "thyme", "tomato", "tulip", "turnip", "vanilla", "verbena", "violet", "walnut", "wheat", "willow",
    "wisteria", "yarrow", "yew", "zinnia",
    "anchor", "bagel", "banjo", "beacon", "bell", "biscuit", "breeze", "brook", "button", "candle", "canoe",
    "canyon", "cello", "cloud", "comet", "compass", "cookie", "creek", "crumpet", "cupcake", "dune", "feather",
    "fjord", "flute", "frost", "glacier", "grove", "harp", "island", "kettle", "kite", "ladder", "lagoon",
    "lantern", "marble", "meadow", "mitten", "moon", "muffin", "noodle", "oasis", "paddle", "pancake", "pebble",
    "piano", "pillow", "planet", "prairie", "pretzel", "puzzle", "quartz", "rainbow", "reef", "ribbon", "ridge",
    "river", "saddle", "sail", "scarf", "shovel", "spoon", "summit", "teapot", "thimble", "thunder", "tide",
    "toffee", "trumpet", "valley", "violin", "waffle", "wagon", "wave", "whistle",
)

TRIES = 8  # fresh codenames tried before one gets a -xxxx added


def codename(choice=secrets.choice):
    """A codename: ADJECTIVE-ANIMAL-THING, each word chosen with CHOICE (secrets.choice)."""
    return f"{choice(ADJECTIVES)}-{choice(ANIMALS)}-{choice(THINGS)}"


def free_codename(taken, choice=secrets.choice, token=secrets.token_hex, tries=TRIES):
    """A codename not in TAKEN (the names in use): TRIES fresh ones, then ones with -xxxx (TOKEN(2)) added."""
    for _ in range(tries):
        name = codename(choice)
        if name not in taken:
            return name
    for _ in range(100):
        name = f"{codename(choice)}-{token(2)}"
        if name not in taken:
            return name
    raise ValueError("no free codename; choose a name with --name")


if __name__ == "__main__":
    print(codename())
