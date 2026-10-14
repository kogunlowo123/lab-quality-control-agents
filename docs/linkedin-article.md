A control result breaks a Westgard rule at 6 a.m. Who stops the patient results, and who proves later why the fix worked?

In many labs the answer is a person reading the analyser screen, a paper CAPA form and a supervisor signature days later. The evidence sits in several systems.

Why it matters: results from a failing method can still be released, and inspectors expect the failure, the corrective action, its verification and the patient lookback linked together.

The flow we built:
QC result (HL7 ORU^R01 or ASTM) → Westgard evaluation → result hold → investigation agent → root cause ranking → corrective action agent → supervisor signature → verification agent → closure → hold lifted → signed audit

How it works:
1. Control results are scored as z-values against the lot target. All six rules run: 1_2s as a warning, 1_3s, 2_2s, R_4s, 4_1s, 10_x, within and across runs and levels.
2. A rejected run places a hold before any agent runs. The LIS reads the hold list.
3. The investigation agent ranks causes from calibration, maintenance and lot changes, faults and other failing analytes. A model may summarise the ranking; it never decides.
4. Agents propose CAPA. Only a supervisor signs, and never their own proposal.
5. Verification rules check the next complete runs before closure.
6. MCP tools and an A2A card let other agents use it; identity comes from our agent identity fabric.

Best practices: refuse any message with patient identifiers; fail closed when identity or audit is unavailable; keep thresholds in code with worked examples as tests.

Tradeoffs: deterministic scoring is explainable but needs instrument events to be recorded. Two-person signing adds a step. Running without a model removes vendor lock-in but leaves supervisors reading raw evidence.

Value: faster recovery, no release from a failing method, inspection evidence in one query.

As labs add more automation, the line between agent assistance and analytical judgement has to be enforced in policy, not left to training.

Question: should an agent ever be allowed to lift a QC hold if verification passes cleanly?

Code: github.com/kogunlowo123/lab-quality-control-agents

#AI #ArtificialIntelligence #GenerativeAI #AgenticAI #LLMOps #EnterpriseAI #AISecurity #RAG #MachineLearning #DataScience #CloudComputing #Kubernetes #MLOps #DigitalTransformation #Innovation #LabMedicine #QualityControl #Westgard
