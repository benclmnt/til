---
Title: Active Learning
tags: [ml]
date: 2021-05-24
---

# Active Learning

Active, because it is able to query instances based on past queries/response

## Overview

Problem: Have access to lots of data, but infeasible to annotate everything.

Key Assumption: if the algorithm were allowed to **choose the data it wants to learn from**,  it could attain a higher level of accuracy while using a smaller number of training labels. 

Solution: The algorithm interactively pose queries (choose an instance of unlabelled data) for human to label. In other words, the algorithm tries to select the best X data to label and learn from it.

## Categories

1. pool-based sampling
2. stream-based selective sampling
3. membership query synthesis

### Pool Based

Assumption: There is a large pool of unlabelled data
Key Idea: Rank the data based on some informativeness measure, and choose the most informative instance to be labelled.
Disadvantage: Need a lot of memory

**Big Fat Question: How to rank?**
Ideally, we want to choose datapoints for which the model is wrong. But the data itself is unlabelled... So, common techniques include:
1. **Uncertainty sampling**: Try to find images the model isn’t certain about, as a proxy of the model being wrong about the image.
2. **Diversity sampling**: Try to find images that represent the diversity existing in the images that weren’t annotated yet. 

**Acquisition Function:** A function that takes in an instance and returns the rank.

Examples of classic acquisition function:
1. The entropy $H(p) = - \sum p_i \log_2(p_i)$. The idea for this function is to rank inputs for which the probability are uniform across all possibility, which indicates that the model are completely confused.
2. Margin Sampling: The difference between the largest output from the model, and the second largest output.
3. Least Confidence / Variation Ratio: $1 - \max(p)$. Search for instance where the model has the least confidence in its most likely label.

### Stream Based

Assumption: There is a large pool of unlabelled data
Key Idea: Stream the data one by one, and for each instance, let the model choose whether it wants to label the instance or not.
Disadvantage: Hard to keep within budget.

### Membership query synthesis

Key Idea: Let the algorithm generate / construct an instance to be labelled.
Disadvantage: Only suitable for problems where it is easy to generate instance

## Usecase

- [Amazon Sagemaker Automated Labelling](https://docs.aws.amazon.com/sagemaker/latest/dg/sms-automated-labeling.html)
