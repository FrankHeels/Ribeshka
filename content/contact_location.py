"""Fixed, owner-approved location; coordinates use the Yandex [lng, lat] order."""
from urllib.parse import urlencode


CONTACT_LOCATION = {
    "address": "г. Гурьевск, ул. Каштановая, 1Б",
    "coordinates": [20.608287, 54.764501],
    "zoom": 18,
    "map_url": "https://yandex.ru/maps/?" + urlencode({
        "ll": "20.608287,54.764501",
        "z": 18,
        "text": "Калининградская область, Гурьевск, Каштановая улица, 1Б",
    }),
    "directions_url": "https://yandex.ru/maps/?" + urlencode({
        "rtext": "~54.764501,20.608287",
        "rtt": "auto",
    }),
}
