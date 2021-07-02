---
Title: Kelly's Criterion
Date: 29 May 2021
math: true
---

# Kelly's Criterion

Assumption: You know the probability of success $p$.

For an investment decision where
1. the probability of success is $p$ 
2. If you succeed, the value of your investment increases from 1 to $1 + b$
3. If you fail (with probability $q = 1-p$), the value of your investment decreases from 1 to $1 - a$

Then the asymptotically optimal fraction of the current bankroll to wager is defined as $$f^* = \frac{p}{a} - \frac{1 - p}{b}$$

From:
- [Wikipedia](https://en.wikipedia.org/wiki/Kelly_criterion)
- [Small game](https://beatkelly.burakyenigun.com/)
