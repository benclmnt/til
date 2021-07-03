---
title: Peeking PayNow
date: 2021-07-03
draft: false
summary: PayNow QR is a unified payment across 9 banks in Singapore. It is interesting to peek at what is actually behind the QR code that power our daily payments
---

## What is QR code?

Put it simply, QR Code is a machine-readable encoding of a text.

## PayNow

PayNow is actually one of the payment types available under SGQR, which follows the international EMV specs.

You can read more about the specifications:
- [PayNow](https://www.dropbox.com/s/5j7c52f9vugs531/paynow.zip?dl=0&file_subpath=%2Fpaynow-qr-specifications.pdf)
- [EMV](https://www.dropbox.com/s/5j7c52f9vugs531/paynow.zip?dl=0&file_subpath=%2FEMVCo-Merchant-Presented-QR-Specification-v1-1.pdf)

If you try to scan any PayNow QR using your phone's text scanner, it will look something like `00020101021226370009SG.PAYNOW010120210T08SS0080L030115204000053037025802SG5910My Company6009Singapore62200116Some Payment Ref63046B56`

## Generating PayNow QR

Here is a gist from [chengkiang](https://www.github.com/chengkiang) to generate the PayNow string. 

{{< gist chengkiang 7e1c4899768245570cc49c7d23bc394c >}}

Some notes:
- The expiry date for any merchant is optional
- If field 54 `amount` is not provided, the app will prompt the user to enter the amount paid
- Field 62 `refNumber` is a way to provide description to transactions
- You can refer to more scenarios for both mobile and UEN in the last page of the PayNow specs

After generating the PayNow string, use any of the libraries or any free websites that provide conversion from text to QR.
