# AI usage
Keep all three headings exactly as they are. If this file is missing or any heading is
missing, the grader stops and gives 0 ("AI_USAGE.md missing or incomplete"). The content
is not scored, but your Track Mentor reads it and the W8 defence can ask about it.
## Tools
ChatGPT
Claude
## What AI got wrong

After i finished the models.py and started with the main.py i told him to write the get_account to make work faster and then after checking his code and also the models, he used customer_id not owner_id which wasn't in the models.py and that's it.
## In my own words
1. Why does a transfer to someone else's account return 404 and not 403?
It's when source account not owned by the caller or not found which is 404 characteristic not 403
2. What would happen to a customer if your idempotency record were written in a separate transaction after the transfer?
If the user did retry later it will be treated as new transfer which will charge him twice if the first one succeeded