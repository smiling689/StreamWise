# Course Project Guidelines

## 1. Grading

The course project accounts for 30% of the final grade. Within the project, the score is split into a base score and a bonus score: the base score is worth 75% and the bonus score 25%. The base score reflects how completely the mandatory tasks are done; the bonus score is the sum of the points for the extension tasks completed, capped at 25 points. Extension tasks may be chosen freely, and doing more is encouraged. (Groups choosing the "student-defined open problem" must submit a problem proposal by mid-term, explaining the significance of the problem and estimating its point value. The instructor and TAs will review it and set the final point value.)

## 2. Format

Students form their own teams; 2 people per team is recommended. There are 10 topics in total, and each topic can be taken by only one team. Topic sign-up sheet: <https://www.kdocs.cn/l/cqPppcMgiToy> (**Do NOT overwrite entries others have already filled in!!!!!**). Posters and demos will be presented in class in Week 16 and graded on site.

## 3. Deliverables

There are two main parts:

1. **Project poster.** Following the given academic-conference poster format, summarize your team's work in a poster.
2. **A submission package**, including the project report, the code, and any demo materials such as screenshots and videos. Specific requirements are listed under each topic.

---

# Topic 8: Streaming Video Transmission System Design and AI-Driven Adaptive Bitrate

## Objectives

1. Master the principles of TCP-based streaming video transmission and segment packaging techniques;
2. Implement dynamic client–server interaction and real-time video playback;
3. Design a basic adaptive bitrate (ABR) algorithm to cope with network fluctuations;
4. Explore efficient path optimization and segment reassembly mechanisms for streaming video;
5. Explore the application of artificial intelligence to congestion control in streaming networks.

## Requirements

### Basic Features (Mandatory)

**1. Server:**

- **Video segmentation:** split long videos into fixed-length segments (e.g., 5 seconds each) and package them as TS files;
- Transmit segments over TCP, with support for handling multiple client requests concurrently;
- Record the send time and bitrate of each segment and generate a transmission log.

**2. Client:**

- **Dynamic segment requests:** repeatedly request segment files from the server (e.g., `ocean-1080p-8000k-0.ts`), constructing names from segment sequence numbers;
- After receiving segments, call a player library for real-time playback;
- **Basic ABR:** implement a simple rule-based ABR algorithm (e.g., based on buffer occupancy or bandwidth measurement) that dynamically switches the requested video bitrate;
- **Local cache management:** temporarily store received segments in a `download` folder and clean them up automatically after playback.

### Extensions

| Extension | Points | Description |
| --- | --- | --- |
| Intelligent ABR | 25 | Replace the rule-based approach with machine learning or reinforcement learning (basic tabular Q-Learning recommended; time-series prediction, DQN, etc. also acceptable) to output bitrate decisions dynamically according to network fluctuations; measure and compare its QoE (Quality of Experience) improvement over the basic algorithm |
| Dynamic path optimization | 25 | Integrate a simple path-finding algorithm to optimize the transmission path, reducing latency and increasing throughput |
| Sliding-window streaming | 15 | Implement a sliding-window reassembly mechanism for segment data to support seamless playback of long video streams |
| End-to-end encrypted transmission | 10 | Encrypt video segments with AES to secure transmission |
| Student-defined open problem | 10–25 | Propose your own extension problem related to "computer network architecture" or "communication efficiency" and provide a code implementation |

## Tech Stack

- **Language:** C/Python (C recommended, for compatibility with the lab framework);
- **Protocols:** TCP (to guarantee segment ordering and integrity); UDP optionally for status monitoring;
- **Tools:** FFmpeg (video segmentation), Wireshark (packet capture and analysis);
- **Storage:** local file system (segment storage), SQLite (transmission logs).

## Suggested Division of Labor

| Module | Scope | Effort |
| --- | --- | --- |
| Server development | Segment generation, concurrent transmission, and log management | 0.5 person |
| Client development | Segment requests, player integration, and caching | 0.5 person |
| Algorithm design | Design and implementation of the ABR and dynamic path optimization algorithms | 0.5 person |
| Testing and tuning | Network fluctuation simulation and performance analysis | 0.5 person |

## Submission Contents

**Code**

- Complete server and client source code;
- Segment processing and ABR algorithm implementation code.

**Documentation**

- **Design document:** system architecture diagram, segment protocol format, ABR algorithm flowchart;
- **Test report:** comparative analysis of rebuffering rate and PSNR under different network bandwidths;
- **User manual:** environment setup steps and player debugging instructions.

**Demo**

- Live demonstration of segment transmission and playback (at least 3 clients running simultaneously);
- Simulate a sudden bandwidth drop and show the ABR switching behavior.
