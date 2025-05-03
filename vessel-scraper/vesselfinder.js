import { DOMParser } from "https://deno.land/x/deno_dom@v0.1.41-alpha-artifacts/deno-dom-wasm.ts";;
import { assert } from "https://deno.land/std@0.201.0/assert/mod.ts";
import { DB } from "https://deno.land/x/sqlite/mod.ts";

const db = new DB("vessels2.db");
db.execute(`
  CREATE TABLE IF NOT EXISTS vessel (
    imo VARCHAR(10) NOT NULL PRIMARY KEY,
    mmsi VARCHAR(10),
    name TEXT,
    vessel_type VARCHAR(255),
    vtyp_id INT, 
    call_sign VARCHAR(16),
    length INT,
    width INT,
    gross_tonnage INT, 
    deadweight_tonnage INT,
    build_year INT,
    country VARCHAR(255),
    country2 VARCHAR(2),
    updated_at datetime NOT NULL DEFAULT current_timestamp
  );
`);

// const twoLetterCountryCode = ['AF', 'AL', 'DZ', 'AS', 'AD', 'AO', 'AI', 'AQ', 'AG', 'AR', 'AM', 'AW', 'AU', 'AT', 'AZ', 'BS', 'BH', 'BD', 'BB', 'BY', 'BE', 'BZ', 'BJ', 'BM', 'BT', 'BO', 'BQ', 'BA', 'BW', 'BV', 'BR', 'IO', 'BN', 'BG', 'BF', 'BI', 'CV', 'KH', 'CM', 'CA', 'KY', 'CF', 'TD', 'CL', 'CN', 'CX', 'CC', 'CO', 'KM', 'CD', 'CG', 'CK', 'CR', 'HR', 'CU', 'CW', 'CY', 'CZ', 'CI', 'DK', 'DJ', 'DM', 'DO', 'EC', 'EG', 'SV', 'GQ', 'ER', 'EE', 'SZ', 'ET', 'FK', 'FO', 'FJ', 'FI', 'FR', 'GF', 'PF', 'TF', 'GA', 'GM', 'GE', 'DE', 'GH', 'GI', 'GR', 'GL', 'GD', 'GP', 'GU', 'GT', 'GG', 'GN', 'GW', 'GY', 'HT', 'HM', 'VA', 'HN', 'HK', 'HU', 'IS', 'IN', 'ID', 'IR', 'IQ', 'IE', 'IM', 'IL', 'IT', 'JM', 'JP', 'JE', 'JO', 'KZ', 'KE', 'KI', 'KP', 'KR', 'KW', 'KG', 'LA', 'LV', 'LB', 'LS', 'LR', 'LY', 'LI', 'LT', 'LU', 'MO', 'MG', 'MW', 'MY', 'MV', 'ML', 'MT', 'MH', 'MQ', 'MR', 'MU', 'YT', 'MX', 'FM', 'MD', 'MC', 'MN', 'ME', 'MS', 'MA', 'MZ', 'MM', 'NA', 'NR', 'NP', 'NL', 'NC', 'NZ', 'NI', 'NE', 'NG', 'NU', 'NF', 'MP', 'NO', 'OM', 'PK', 'PW', 'PS', 'PA', 'PG', 'PY', 'PE', 'PH', 'PN', 'PL', 'PT', 'PR', 'QA', 'MK', 'RO', 'RU', 'RW', 'RE', 'BL', 'SH', 'KN', 'LC', 'MF', 'PM', 'VC', 'WS', 'SM', 'ST', 'SA', 'SN', 'RS', 'SC', 'SL', 'SG', 'SX', 'SK', 'SI', 'SB', 'SO', 'ZA', 'GS', 'SS', 'ES', 'LK', 'SD', 'SR', 'SJ', 'SE', 'CH', 'SY', 'TW', 'TJ', 'TZ', 'TH', 'TL', 'TG', 'TK', 'TO', 'TT', 'TN', 'TR', 'TM', 'TC', 'TV', 'UG', 'UA', 'AE', 'GB', 'UM', 'US', 'UY', 'UZ', 'VU', 'VE', 'VN', 'VG', 'VI', 'WF', 'EH', 'YE', 'ZM', 'ZW', 'AX']
const twoLetterCountryCode = ['ID']
const vesselType = [
    // 401, 402, 403, 404, 405, 406, 407, 408, 409, 410, 411, 412, 413, 414, // cargo
    // 601, 602, 603, 604, 605, 606, 607, 608, 609, // tanker
    // 301, 302, 303, 304, // passengers
    // 5, // fishing ships
    // 8, // yacht
    // 7, // military
    // 2, // high speed crafts
    0, // other type or auxiliary
    // 1, // unknown
]
const addVesselQuery = db.prepareQuery(
    "INSERT INTO vessel (imo, name, vessel_type, vtyp_id, length, width, gross_tonnage, deadweight_tonnage, build_year, country, country2) VALUES (:vimo, :vname, :vtype, :vtyp_id, :vlength, :vbeam, :vgrossTonnage, :vDwt, :vbuild, :vcountry, :vcountry2)",
);

function toInt(s) {
    let ret = parseInt(s)
    return ret ? ret : null
}

for (let typ of vesselType) {
    for (let cc of twoLetterCountryCode) {
        let maxPage = 200
        for (let page = 1; page <= maxPage; page++) {
            console.log(`Fetching page ${page} for country ${cc} and type ${typ}...`);
            let resp = await fetch(`https://www.vesselfinder.com/vessels?page=${page}&type=${typ}&flag=${cc}&minYear=2010`)
            const document = new DOMParser().parseFromString(await resp.text(), "text/html");
            assert(document);
            const table = document.querySelector("table.results");
            if (!table) {
                console.log("No vessel table found");
                break;
            }
            if (page == 1) {
                let mp = document.querySelector(".pagination-controls").querySelector("span").innerHTML.split(" / ").at(-1)
                if (mp > 200) {
                    console.warn(`max page = ${mp} for country ${cc} and type ${typ}`)
                } else {
                    maxPage = mp
                }
            }
            const rows = table.querySelectorAll("tr");
            for (let row of rows) {
                if (row.querySelector("th")) {
                    // we skip the title.
                    continue
                }
                const vimo = row.querySelector(".ship-link").getAttribute("href").split("/").at(-1)
                const vname = row.querySelector(".slna").innerHTML;
                const vtype = row.querySelector(".slty").innerHTML;
                const vbuild = toInt(row.querySelector(".v3").innerHTML);
                const vgrossTonnage = toInt(row.querySelector(".v4").innerHTML);
                const vDwt = toInt(row.querySelector(".v5").innerHTML);
                const vSize = row.querySelector(".v6").innerHTML;
                const vlength = toInt(vSize.split(" / ")[0]);
                const vbeam = toInt(vSize.split(" / ")[1]);
                const vcountry = row.querySelector(".flag-icon").getAttribute("title");

                try {
                addVesselQuery.execute({ vimo, vname, vtype, vtyp_id: typ, vbuild, vgrossTonnage, vDwt, vlength, vbeam, vcountry, vcountry2: cc });
                } catch {}
            }
        }
    }
}
addVesselQuery.finalize();
db.close();