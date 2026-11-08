"""ERA5 pressure-level fields from the public ARCO-ERA5 archive, in the GRIB layout PyAPS reads.

Why this exists. MintPy's tropospheric correction (PyAPS with ERA5) downloads one weather
file per acquisition date from the Copernicus Climate Data Store. The CDS serves requests
through a shared queue that can hold a single request for hours, needs an account, and
refuses more than a few queued requests per dataset, so a correction that needs 30 to 90
files can take a day or stall. ARCO-ERA5 is Google's public, analysis-ready copy of the same
ERA5 reanalysis (Hersbach et al. 2020): no account, no queue, about 10 seconds per date.

Checked against a file the CDS produced for the same date and grid: the largest differences
are the GRIB packing precision (0.7 m2/s2 on geopotential with a range of 14000, 0.0008 K on
temperature, 3e-7 on specific humidity).

`retrieve` accepts the request PyAPS would send to the CDS and writes the same kind of GRIB
file: for every pressure level, geopotential, temperature and specific humidity, on the
0.25 degree grid, cut to the requested area. This file does not import the rest of the
package, because `cds_shim/cdsapi` loads it by path inside MintPy's own Python process.
"""
from __future__ import annotations

import base64
import datetime as dt
import os

import numpy as np

ARCHIVE_URL = "https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
_STEP = 0.25
_EPOCH = dt.datetime(1900, 1, 1)
# CDS variable name -> ECMWF parameter id, in the order PyAPS reads them for each level.
_VARIABLES = (("geopotential", 129), ("temperature", 130), ("specific_humidity", 133))

# One real GRIB message (edition 1, regular lat/lon, 16 bit simple packing) as the CDS writes
# it. Every output message is a copy with the grid, level, parameter, date and values replaced.
_TEMPLATE = base64.b64decode(
    "".join(
        """
R1JJQgAaXgEAADSAYpH/gIFkAAEXAQMSAAEAAAAAAAAVAAAAAAAAAAAAAAAAAAAAARcCBAEwMDAxAAAAAAAgAP8A
ACkAUQDDUIAnEIAAdTAAAAAA+gD6AAAAAAAAGf4IgAJFbqEeEBsAGVgXwBZIFQgUCBNIEtASkBKAEngScBJYEiAR
0BF4ESgQ6BC4EJgQeBBAD/APeA7QDfgM8AuwCkgIsAbwBRgDWAHQALgAIAAAAFgA+AGwAigg2B9oHfgcmBtgGmAZ
kBkAGKAYaBhIGDAYCBfYF6AXaBc4FxgXEBcQFwAW0BZ4FegVGBQQEtgRaA/YDigMYAqoCRAHyAbwBpgGuAc4B+gI
gAjYJmAlGCPQIpAhcCBwH6AfAB6QHkgeGB34HdAdsB2YHYgdiB2QHagdsB2gHVgc0BwIGwAZuBhIFrAVABNIEaAQ
EA7ADcANKAz4DTANoA4gDngOeCugKogpaChQJ1AmaCWgJQgkmCRIJBAj6CPQI8gjyCPgJAgkMCRIJEAkACN4IqAh
iCAwHrAdGBtoGcAYIBagFVAUQBOAExAS8BMIEzgTaBNoExgwwC/gLvguEC04LGgrwCswKsAqaCowKggqACoIKigq
UCp4KpAqgCpAKbAo0CeoJkAksCMQIWgfyB5AHNAbiBpoGYAY4Bh4GDgYGBgIF+gXsBdANgg1WDSgM+gzMDKAMegx
YDD4MKAwaDBAMDgwQDBYMHAwcDBQL/gvWC5wLUgr4CpQKLAnGCWIJBAisCFwIFAfWB6AHeAdaB0IHMAcgBxAG/gb
mDt4OvA6YDnAOSg4kDgIN4g3IDbINoA2UDYwNhg2CDXgNaA1KDSAM5gycDEgL6guICyYKxgpsChoJzAmGCUQJCgj
YCKwIiAhqCFIIQAgyCCYIGBBAECQQBA/kD8IPoA9+D14PQA8mDw4O+A7kDtAOuA6cDngOSg4QDcwNgA0uDNgMggw
sC9oLigtACvgKtgp4Cj4KCgncCbYJmgmECXYJcAlsCWgRmBGCEWYRSBEoEQYQ5BDAEJwQeBBUEDAQCg/kD7oPjg9
eDygO7A6sDmgOIA3YDZINSg0CDLoMcgwuC+wLrAtyC0ALEgruCtQKxAq8Cr4KwgrCEuQSzhK2EpgSdhJSEioR/BH
OEZ4RahE2EQQQ0BCeEGwQOhAID9QPoA9qDzAO9A62DnYOMg3sDaYNYA0cDNwMoAxuDEQMJgwQDAgMCAwODBYMGhQ
aFAYT6hPKE6ITdhNCEwwS0hKWElgSHBHkEa4RfhFSESgRAhDaELAQhBBSEBoP3A+aD1QPDA7CDnwOOA36DcINlA1
yDVoNTg1MDVINXA1kDWgVPBUgFP4U0hSiFGoULhPuE64TbhMyEvgSxhKYEnISUBIwEhIR8BHKEZwRaBEsEOgQoBB
WEAwPxA+AD0IPDA7cDrgOng6QDowOkA6aDqYOrg6wFjwWFBXmFa4VdBU2FPYUthR6FEAUDhPgE7gTlBN2E1oTPBM
cEvYSyBKSElYSEhHKEYAROBD0ELQQehBKECAQAA/oD9wP2A/cD+QP8A/4D/wP+hcUFt4WpBZmFigV6hWuFXgVRhU
aFPIU0BSwFJAUchRSFDAUBhPYE6ITZhMkEt4SmhJWEhoR4hGyEYwRbBFSEUIROBE0ETYRPhFGEU4RUhFQEUgXyhe
KF0wXDBbSFpwWahY+FhgV9hXYFboVnBV6FVgVMBUGFNYUohRqFC4T8hO0E3oTSBMcEvYS2BLCErISqBKiEqASpBK
oErAStBK2ErQSrBKcGGwYLBfuF7YXghdUFy4XChbsFs4WshaWFnYWUhYuFgQV2BWsFXoVSBUUFOIUshSGFGIURhQ
wFCIUGBQUFBIUFBQWFBoUHhQgFCAUHBQSFAQT7hkOGNQYnhhsGEAYGBf2F9oXvhemF4wXcBdSFzIXEBbsFsYWnhZ
2FkoWIBX2FdAVsBWYFYYVfBV2FXYVeBV8FYIVhhWIFYgViBWCFXgVahVUFTgZvhmMGVoZLhkIGOYYyhiyGJ4YiBh
0GGAYSBguGBIX9BfUF7IXjhdmF0AXGhb6FuAWzhbEFsAWxBbIFtAW2BbgFuQW5hbmFuIW2BbKFrQWlhZyGoAaUho
oGgIZ4hnGGbIZohmUGYgZehlsGVoZRhksGRAY8BjOGKYYfhhWGDIYFhgCF/gX9Bf6GAQYDhgcGCgYMBg2GDgYNBg
sGCAYCBfqF8QXkhtUGywbCBroGtAawBq0GqwapBqeGpQaiBp2GmAaRBoiGfwZ1BmqGYAZXBk8GSYZGhkYGR4ZLBk
8GUwZXhluGXgZgBmCGXwZcBlaGTgZChjUGJQcOhwYG/wb5hvYG84byBvEG8AbuBusG5obgBtiG0AbGBruGsYanBp
4GloaRBo4GjYaOhpIGlwacBqGGpwarhq6GsIawBq0GpwaehpGGggZwhl0HTIdGB0CHPIc6BzgHNoc1BzMHLwcqBy
OHG4cShwiG/ob0huuG44bdBtgG1YbUhtWG2IbdBuKG6QbvBvUG+gb9Bv2G+4b1huwG3wbOBroGpYaPh42Hh4eCh3
6He4d5B3YHcoduh2kHYgdaB1GHSIc/hzaHL4cpByQHIAceBx0HHYcfhyMHJ4cthzQHOwdAh0UHRwdFB0AHNYcnBx
WHAIbphtOGvYfNh8cHwQe8B7cHsweuh6mHpAeeB5cHj4eIB4CHeYdzh26HawdoB2YHZQdlB2WHZ4drB3AHdgd8h4
MHiAeKh4mHhAd6B2sHWAdDByyHFYb/huwICYgBh/oH8wfsh+cH4gfdB9gH0wfNh8iHwwe9h7mHtYeyh7AHroesh6
uHqwerh60HsAe1B7qHwIfFB8eHxofBh7eHqYeXh4IHa4dVBz+HLAcbiECINwgtiCYIH4gaiBYIEwgQCA0ICYgHCA
OH/4f8B/mH9gfzh/EH7gfsB+sH6wfsh/AH9If4h/yH/wf+B/mH8IfjB9IHvgeoB5IHfgdsB1yHUIh0CGmIYIhaCF
SIUQhPiE6ITghMiEsISQhGiEKIPog6iDaIMggtiCoIJwgmCCYIJ4grCC6IMYgziDMILwgmiBsIDAf5B+WH0Ye+B6
0HnoeSh4mIp4ieCJaIkgiPCI2IjgiPCI6IjYiMiImIhYiACHqIdIhvCGoIZQhhiF+IXohfCGCIY4hmCGgIZ4hkiF
2IUwhFiDUIIogPh/0H7Afdh9KHyofFiN2I1gjRCM6IzYjNiM8Iz4jOiM0IyojFCL6It4iwCKgIoYibiJaIkwiRiJ
IIlAiXCJoInIidCJqIlQiMCIAIcQhgCE6IPQgtiCCIFwgQiAyICQkXCRGJDgkNCQ0JDQkNCQyJCgkHCQII+wjzCO
wI5AjcCNWI0IjMCMmIyQjKiM2I0QjTCNQI0wjOiMcIvAiuiJ6Ijgh9iG6IYYhYiFKITghLCEgJUglOCUuJSglKCU
kJR4lFCUGJPIk2CS6JJwkfiReJEAkKiQaJA4kCiQOJBYkICQoJC4kKiQcJAIj2iOoI24jMCL0IrwiiiJiIkgiNiI
sIigiJCYwJiImFiYQJgomBCX6Jewl2iXEJaYliCVqJUwlLiUWJQQk9CTqJOgk7CTyJPgk/iT+JPIk3CS6JI4kWiQ
iI+wjuiOOI2wjVCNII0QjRCNEI0AnDCcAJvQm7CbkJt4m0Ca+JqwmlCZ6JlwmPiYgJgYl8CXcJc4lxiXCJcQlyiX
MJcolwiWyJZYlciVIJRgk5iS4JJAkciRcJFAkTCROJE4kSiQ+J94n0ifIJ8AnuCeyJ6Qnkid8J2YnTCcwJxAm8Cb
UJrwmpCaWJo4mjCaMJpAmkCaKJoAmbiZSJi4mBiXaJbIljCVwJV4lViVSJVIlVCVSJUglNiimKJwoliiQKIgogCh
0KGQoTCguKBIn9CfQJ64nkid6J2YnXCdWJ1QnVidYJ1YnTidCJywnDibsJsgmpCaEJmomViZMJkomSiZMJkwmRCY
yJhYpaClgKVwpVilQKUgpOikmKQwo7CjMKKoohihkKEooNCgkKBwoGigaKBwoHigaKBIoAifqJ8wnrieQJ3AnVid
EJzgnMicyJzInMCcoJxonBCbkKiIqHioaKhYqECoGKfQp3CnAKZ4pfClaKTgpGikEKPIo5ijiKOIo4ijkKOAo2Cj
KKLoopCiKKG4oVCg6KCQoFigOKAooCCgGKAAn9CfkJ8wnqCrWKtQq1CrOKsYquiqmKooqbipMKigqCCnqKdIpvim
yKaoppimkKaApnimaKY4pfilsKVgpQCkmKQ4o+ijqKNwo1ijSKMwoxii+KLAonCiCKF4riCuGK4YrgCt2K2YrTis
yKxQq9CrUKrgqniqIKngqbipmKlwqVCpOKkgqQCo4KioqGioIKfIp3CnEKbIpoimSKYopiCmAKXopdClmKVApNCk
SLDYsNCwyLCosGiwIK/Ar1Cu2K5grfCtmK1ArPCsuKyIrGCsOKwYq/ir0Kugq3CrQKsIqsCqcKoYqcCpeKkwqPio
2KjIqLCooKiIqEin6KdopsCzcLNgs0CzELLQsniyGLGwsUCw4LCIsDiv6K+Yr1CvEK7QrpCuaK5AriCuAK3grbit
gK1ArPCsmKxAq/CrsKuAq2irYKtQqzirGKrQqlipuKjwteC1uLWItVC1CLSwtFi0ALOos1izCLKwsmCyCLGwsVix
CLDAsIiwaLBQsECwMLAQr+CvmK9IrvCuoK5YriCuAK3oreCt0K2wrXCtCKxwq7CqyLgAt9i3mLdYtxC20LaItkC1
8LWotVC0+LSYtDCz0LNwsyiy8LLIsrCymLKIsnCySLIYsdCxgLEwsOiwsLCIsHCwYLBIsCCv4K94rvCuQK1orHi5
8LnAuYi5ULkQuNi4oLhguBi3wLdgtwC2kLYgtbi1YLUYtOi00LTAtLi0sLSYtHC0OLP4s7izeLM4swiy4LK4spiy
aLIYsbixOLCgr+CvEK4wu7i7iLtYuzC7ALrQupi6WLoIuai5OLjIuFi36LeItzi3CLbottC2yLbItri2oLZ4tki2
CLXQtZi1YLUwtQi0yLSItDiz0LNYssiyKLFwsMCwAL1ovUC9ILz4vNC8oLxgvBi7uLtIuti6YLn4uZC5QLkIuOC4
0LjQuMi4wLiouIi4YLg4uAC30Legt2C3KLbotpC2MLXQtVi02LRYs8izMLKYsfi/AL7ovsi+qL54vkC9+L2YvTC8
yLxQu+i7gLswuvi62LrAuri6sLqoupi6gLpYujC6ALnQuaC5cLkwuPC4mLgwt8i3YLbotmi18LV4tPC0cLPYwIjA
cMBQwCi/8L+ov1i++L6Qvii9wL1ovRi84Ly4vKi8mLyQvIi8cLxQvDC8CLvgu7i7kLtguyC64LqQuji5yLlYuPC4
eLgIt5i3GLaYthC1gMHwwdjBsMGAwUDA8MCgwEC/4L+AvzC+6L64vpC+eL5wvmC+UL5AviC98L3IvZi9cL1IvSi9
ALzIvIi8OLvQu2C68Lp4ugi5mLkguJi4ELeAtujDQMMgwvDCuMJwwiDB0MF4wSjA4MCowHjAWMBIwDjAMMAYwAC/
2L+ov3i/SL8gvvi+yL6gvoC+UL4IvbC9ULzovHi8ALt4uvi6cLnguVC4uLgYxIDEWMQgw+DDmMNQwwDCsMJ4wkjC
IMIQwgDB8MHowdDBsMGIwVDBIMDwwMDAoMCQwHDAUMA4wAi/uL9YvuC+aL3gvVC8uLwou5C68LpYucC5KMWwxXjF
QMT4xLDEaMQow/DD0MO4w6DDoMOYw4jDeMNYwyDC8MLAwojCaMJIwijCGMHwwcjBqMFowRDAuMBIv7i/GL54vdC9
MLyQu/C7YLrQukDGyMaIxkjGCMXAxYjFYMU4xSjFIMUYxRjFEMT4xODEsMR4xEjEGMPow9jD0MPIw7jDmMNowzjC
4MJgwejBaMDIwCi/eL7IviC9gLzovGC74Ltox8jHkMdIxxDG2MaoxpDGgMaAxoDGgMaAxnDGUMYoxfDFwMWgxYDF
YMVYxVDFOMUoxQDEyMSYxEDDuMMowojB0MEgwHC/uL8YvoC98L14vQi8qMjAyIjISMgYx/DH0MfIx8jH0MfQx9DH
yMeox4DHWMcwxwjG8MbgxtDG0Ma4xojGUMYIxbDFYMUAxIjECMNwwsDCCMFYwLDAEL+Ivwi+qL5QvgDJuMmAyVDJ
KMkIyQDJAMkAyQjJCMkAyPDI0MigyIDIWMhAyDDIKMgYyBDH8Me4x3DHKMbIxmjF8MVoxODEQMOYwvDCSMGowSDA
oMA4v+i/oL9gyqjKeMpYyjjKKMogyiDKKMooyijKGMoAyeDJuMmYyYDJcMlwyWjJUMkwyQDIwMhoyAjHqMdAxsjG
QMWwxRDEcMPQwzjCsMI4wdDBeME4wQDA2Mugy3jLYMtIy0DLOMs4y0DLQMs4yyjLCMroysjKqMqYypDKiMp4ymDK
OMn4yaDJQMjgyHDH+MeAxvjGcMXgxUjEuMQww8DDWMMAwsDCiMJgwkDMkMx4zFjMSMxAzEDMOMxAzDjMKMwYzADL
4MvAy6jLoMuYy4jLcMtQyxjKwMpoygjJqMlAyMjISMfIx0DGsMYwxbDFOMTYxIjEQMQIw+DDuMOYzXjNYM1QzUDN
OM04zTDNMM0ozRjNCMzozMjMsMygzJDMiMx4zFjMKMvoy6DLSMroynjKAMmIyRDImMgQx5DHGMawxkjF+MWwxXDF
SMUoxQDE4M5QzkDOMM4oziDOGM4YzhDOCM34zeDNyM2wzZjNiM2AzXDNUM0wzQDMwMx4zBjLsMtAytjKaMnwyXjJ
AMiQyCDHwMdgxxjG2MagxnjGWMY4xhDPKM8QzwjPAM74zvjO8M7wzuDO0M64zqDOiM54zmjOYM5QzjjOEM3YzZDN
OMzYzHjMEMuwy1DK4MpwyfjJiMkoyMjIeMg4yADH0Megx3jHWMcwz/DP4M/Yz9DPyM/Iz8DPuM+oz5jPgM9oz1jP
SM9AzzjPKM8YzvDOuM54zjDN2M2IzSjMuMxQy+DLcMsIypjKOMnYyYjJUMkQyODIuMiIyGDIONC40KjQoNCY0JDQ
kNCA0HjQaNBQ0EDQMNAg0CDQINAY0AjP+M/Qz6jPeM8ozuDOkM4gzcDNYMzgzHDMCMuYy0DK6MqYyljKIMnoycDJ
kMlgyTjRgNF40WjRYNFY0VDRSNE40SjRGNEA0PjQ8NDw0PjQ8NDw0ODQuNCY0GDQEM/gz5jPGM7AznjN+M2AzSDM
oMxAy/DLmMtYyyDK4Mq4yojKUMow0kjSONIw0iDSGNIQ0gDR8NHg0dDRyNHI0cDRyNHQ0cjRyNG40aDRiNFQ0PjQ
4NCg0BDPsM94zvDOeM4YzaDNMMzgzIjMQMwIy8jLmMtwy0DLGNMY0wjS+NLo0tjS0NLA0rDSoNKY0ojSiNKI0pDS
mNKg0qDSkNJw0mDSKNHY0bjReNDw0JDQSM/Az0DO6M54zhDNuM1gzRjM4MygzHjMSMwgzADT4NPQ08DTqNOY04DT
cNNg01DTSNNA00DTSNNQ01jTWNNY00jTKNMY0uDSkNJg0hjRoNE40OjQgNAQz6DPOM7YznjOKM3gzajNcM1IzSDN
AMzg1KjUkNR41GDUUNQ41CjUGNQA0/DT6NPg0+jT+NQA1ADT+NPg08DToNNo0xjS0NKI0jDR0NFw0RDQqNA4z9jP
gM8ozuDOoM5gzjjOEM3wzdDNsNVg1UDVKNUI1PDU2NTA1KjUmNSI1IjUgNSA1IDUgNSA1HjUaNRA1BDT2NOQ00jS
8NKg0kjR6NGI0SDQwNBo0BjPyM+Iz1DPGM7wztDOqM6IzmjV+NXY1bjVoNWA1WjVUNU41SjVGNUQ1RDVENUI1PjU
6NTY1LjUmNRo1DjT+NOo01DS8NKg0kDR6NGY0UDQ8NCo0GjQMM/4z9DPqM+Az1jPOM8Q1ojWYNZA1iDWCNXw1djV
wNW41ajVmNWI1XjVcNVo1VjVSNUY1ODUqNRo1BjT2NOI00DS8NKg0lDSCNHA0XjRQNEQ0NjQoNB40FDQKNAAz9DP
oNcA1tjWwNao1pDWeNZg1kjWONYo1iDWGNYI1fDV0NWo1ZDVcNU41QDUuNRo1CDT2NOQ00jTCNLA0oDSQNII0dDR
oNFw0UjRGNDw0MDQkNBg0DDXgNdg10jXONcg1xjXENcA1ujWyNao1ojWcNZw1ljWINXw1cDVkNVg1RjU0NSQ1EDT
+NO403jTONMA0sjSkNJg0jDSANHY0ajRgNFQ0RjQ6NC42AjX8Nfg19DXuNeY14DXaNdY12DXUNcg1vjW2NbI1qjW
eNZA1fjVuNV41TDU6NSo1GjUMNPw07DTgNNI0xjS6NK40ojSYNIw0gDR0NGY0WjRONio2JDYiNiQ2IjYiNiQ2GDY
MNgo2BDX8NfY15jXUNcg1vDWuNZ41jDV8NWw1WjVKNTg1KjUaNQw1ADT0NOY02jTONMI0tjSoNJw0kDSENHY0ajZ
WNlA2SjZKNkI2RjZUNk42RDZANio2FjYUNgo1+DXsNdw1zjXANaw1nDWKNXo1ajVaNUo1PDUuNSA1EjUENPg06jT
cNNA0xDS2NKo0njSSNIY2gjZ+Nnw2fDZwNmQ2XjZONkw2XDZQNjo2MjYmNhg2DjX+New14DXONbw1rDWaNYo1ejV
qNVo1TDU+NS41IDUSNQQ09jTqNNw00DTENLg0rDSiNqw2qDakNqg2pDagNpo2iDZ+NoI2djZoNmA2TjY+NjA2HjY
MNf417DXcNcw1ujWqNZg1iDV4NWg1WjVKNTo1LDUeNRA1AjT0NOg03DTQNMY0vDbSNs42xja+Nrg2uja+Nrw2tDa
oNpg2ijaANnI2YjZUNkI2MjYgNg41/DXoNdY1xjW0NaQ1kjWCNXI1ZDVUNUQ1NjUoNRo1DjUCNPY06jTgNNY29Db
wNuw26DbiNto20jbKNsY2wja4Nq42ojaSNoY2djZkNlQ2QDYuNhw2CDX0NeI1zjW+Naw1nDWMNXw1bDVeNU41QDU
0NSY1HDUQNQY0/DTyAA3Nzc3
""".split()
    )
)


def archive_reachable(timeout: float = 5.0) -> bool:
    """Whether the archive answers and the packages needed to read it are installed."""
    try:
        import fsspec  # noqa: F401
        import pygrib  # noqa: F401
        import requests
        import zarr  # noqa: F401

        # The store has been published in both zarr formats under this name: v3 keeps
        # `zarr.json` at the root, v2 keeps `.zmetadata`. Either one means it is there.
        return any(
            requests.get(f"{ARCHIVE_URL}/{name}", timeout=timeout).status_code == 200
            for name in ("zarr.json", ".zmetadata")
        )
    except Exception:
        return False


def _open_archive():
    import zarr

    store = zarr.storage.FsspecStore.from_url(ARCHIVE_URL, read_only=True)
    return zarr.open_group(store=store, mode="r")


def _grid(area, lats_all: np.ndarray, lons_all: np.ndarray):
    """Row and column indices of the archive grid for the area [north, west, south, east]."""
    north, west, south, east = (float(v) for v in area)
    lats = np.arange(north, south - 1e-9, -_STEP)
    lons = np.arange(west, east + 1e-9, _STEP)
    rows = np.rint((lats_all[0] - lats) / _STEP).astype(int)
    columns = np.rint(np.mod(lons, 360.0) / _STEP).astype(int) % len(lons_all)
    if rows.min() < 0 or rows.max() >= len(lats_all):
        raise ValueError(f"area {area} is outside the ERA5 grid")
    return lats, lons, rows, columns


def _message(lats, lons, level: int, param_id: int, date: int, hour: int, values: np.ndarray) -> bytes:
    import pygrib

    message = pygrib.fromstring(_TEMPLATE)
    message["Ni"], message["Nj"] = len(lons), len(lats)
    message["latitudeOfFirstGridPointInDegrees"] = float(lats[0])
    message["latitudeOfLastGridPointInDegrees"] = float(lats[-1])
    message["longitudeOfFirstGridPointInDegrees"] = float(lons[0])
    message["longitudeOfLastGridPointInDegrees"] = float(lons[-1])
    message["paramId"] = param_id
    message["level"] = level
    message["dataDate"] = date
    message["dataTime"] = hour * 100
    message.values = values
    return message.tostring()


def retrieve(request: dict, target, archive=None) -> None:
    """Write the GRIB file the CDS would have returned for `request` (one date, one hour).

    `request` is the dictionary PyAPS sends: variable, year, month, day, time,
    pressure_level and area = [north, west, south, east]. `archive` is for tests.
    """
    year, month, day = int(request["year"][0]), int(request["month"][0]), int(request["day"][0])
    hour = int(str(request["time"][0]).split(":")[0])
    when = dt.datetime(year, month, day, hour)
    wanted = {name for name in request["variable"]}
    missing = wanted - {name for name, _ in _VARIABLES}
    if missing:
        raise ValueError(f"unsupported ERA5 variables: {sorted(missing)}")

    group = archive if archive is not None else _open_archive()
    levels_all = np.asarray(group["level"][:])
    lats, lons, rows, columns = _grid(request["area"], np.asarray(group["latitude"][:]), np.asarray(group["longitude"][:]))
    levels = [int(v) for v in request["pressure_level"]]
    level_index = [int(np.flatnonzero(levels_all == level)[0]) for level in levels]
    hours = int((when - _EPOCH).total_seconds() // 3600)

    fields = {}
    for name, _ in _VARIABLES:
        block = np.asarray(group[name][hours, :, rows.min():rows.max() + 1, :], dtype="float64")
        block = block[level_index][:, rows - rows.min(), :][:, :, columns]
        if not np.isfinite(block).all():
            raise RuntimeError(f"ERA5 has no {name} for {when:%Y-%m-%d %H:%M} in the ARCO-ERA5 archive yet")
        fields[name] = block

    target = os.fspath(target)
    temporary = f"{target}.part"
    with open(temporary, "wb") as handle:
        for i, level in enumerate(levels):
            for name, param_id in _VARIABLES:
                handle.write(_message(lats, lons, level, param_id, year * 10000 + month * 100 + day, hour, fields[name][i]))
    os.replace(temporary, target)
