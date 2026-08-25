import requests

query = """
[out:json][timeout:25];
node["shop"="car_repair"](31.3000,74.1000,31.7000,74.5500);
out tags 5;
"""

headers = {
    "User-Agent": (
        "LeadForgeSprintBot/1.0 "
        "(student internship project; "
        "contact: noor.ul.huda@logitrixsolutions.com)"
    ),
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
}

endpoints = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

for endpoint in endpoints:

    print()
    print("=" * 60)
    print(endpoint)
    print("=" * 60)

    try:

        response = requests.post(
            endpoint,
            data={"data": query},
            headers=headers,
            timeout=30,
        )

        print("Status:", response.status_code)
        print("Body:", response.text[:500])

    except requests.exceptions.RequestException as exc:

        print("Request failed:", exc)