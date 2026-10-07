"""Shared target runtime support; launch and evidence stay in the game adapter."""


_SHARED_GAME_SESSION = r'''package modport.harness;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.LinkedHashSet;

/**
 * One live game session for a compatible ordered group of target cases.
 * A version-specific adapter schedules actions on the game thread and owns
 * reset isolation. JUnit wrappers call requirePassed(id) on the same instance;
 * subsequent wrappers reuse individual receipts instead of launching Gradle.
 * This class does not assert that a game ran: the host verifies separate XML
 * results and live operation-level evidence written by EvidenceWriter.
 */
public final class SharedGameSession<S> {
    public interface Adapter<S> {
        S start() throws Exception;
        void resetBefore(S session, String testId) throws Exception;
        void resetAfter(S session, String testId) throws Exception;
        void close(S session) throws Exception;
    }

    public interface Action<S> {
        Observation execute(S session) throws Exception;
    }

    public interface EvidenceWriter {
        // Write the case's real runtime record and log witness. A diagnostic
        // or a successful reset is never a substitute for runtime observations.
        void write(String testId, Observation observation) throws Exception;
    }

    public static final class Observation {
        public final Map<String, Boolean> assertions;
        public final Map<String, Object> runtime;
        public Observation(Map<String, Boolean> assertions, Map<String, Object> runtime) {
            if (assertions == null || assertions.isEmpty() || runtime == null || runtime.isEmpty()) {
                throw new IllegalArgumentException("assertions and live runtime observations are required");
            }
            this.assertions = Collections.unmodifiableMap(new LinkedHashMap<>(assertions));
            this.runtime = Collections.unmodifiableMap(new LinkedHashMap<>(runtime));
        }
    }

    public static final class Case<S> {
        public final String testId;
        public final Set<String> assertionIds;
        public final Action<S> action;
        public Case(String testId, List<String> assertionIds, Action<S> action) {
            if (testId == null || !testId.matches("[A-Za-z0-9_.:-]+") || action == null
                    || assertionIds == null || assertionIds.isEmpty()
                    || assertionIds.stream().anyMatch(id -> id == null || !id.matches("[A-Za-z0-9_.:-]+"))
                    || new LinkedHashSet<>(assertionIds).size() != assertionIds.size()) {
                throw new IllegalArgumentException("case ID, unique assertion IDs and action are required");
            }
            this.testId = testId;
            this.assertionIds = Collections.unmodifiableSet(new LinkedHashSet<>(assertionIds));
            this.action = action;
        }
    }

    public static final class Receipt {
        public final String testId;
        public final boolean passed;
        public final Throwable failure;
        public final Observation observation;
        private Receipt(String testId, Throwable failure, Observation observation) {
            this.testId = testId;
            this.passed = failure == null;
            this.failure = failure;
            this.observation = observation;
        }
    }

    private final Adapter<S> adapter;
    private final EvidenceWriter writer;
    private final List<Case<S>> cases;
    private final Map<String, Receipt> receipts = new LinkedHashMap<>();
    private boolean executed;

    public SharedGameSession(Adapter<S> adapter, EvidenceWriter writer, List<Case<S>> cases) {
        if (adapter == null || writer == null || cases == null || cases.isEmpty()) {
            throw new IllegalArgumentException("adapter, evidence writer and cases are required");
        }
        Set<String> ids = new LinkedHashSet<>();
        for (Case<S> test : cases) {
            if (test == null || !ids.add(test.testId)) {
                throw new IllegalArgumentException("cases must have distinct test IDs");
            }
        }
        this.adapter = adapter;
        this.writer = writer;
        this.cases = Collections.unmodifiableList(new ArrayList<>(cases));
    }

    /** Run once; absent cases and reset/start/close failures can never pass. */
    public synchronized Map<String, Receipt> run() {
        if (executed) return Collections.unmodifiableMap(new LinkedHashMap<>(receipts));
        // Set this before callbacks to reject reentrant wrapper execution.
        executed = true;
        S session = null;
        Throwable unavailable = null;
        try {
            session = adapter.start();
            if (session == null) throw new IllegalStateException("adapter started no live session");
        } catch (Exception | AssertionError error) {
            unavailable = error;
        }
        for (Case<S> test : cases) {
            if (unavailable != null) {
                receipts.put(test.testId, new Receipt(test.testId, unavailable, null));
                continue;
            }
            Observation observation = null;
            Throwable failure = null;
            boolean entered = false;
            try {
                entered = true;
                adapter.resetBefore(session, test.testId);
                observation = test.action.execute(session);
                if (observation == null || !observation.assertions.keySet().equals(test.assertionIds)) {
                    throw new AssertionError("missing or unexpected assertion results: " + test.testId);
                }
                if (observation.assertions.values().stream().anyMatch(value -> !Boolean.TRUE.equals(value))) {
                    throw new AssertionError("required assertion failed: " + test.testId);
                }
            } catch (Exception | AssertionError error) {
                failure = error;
            } finally {
                if (entered) {
                    try {
                        adapter.resetAfter(session, test.testId);
                    } catch (Exception | AssertionError error) {
                        if (failure == null) failure = error;
                        else failure.addSuppressed(error);
                        // Failed isolation makes the rest of this group unsafe.
                        unavailable = error;
                    }
                }
            }
            if (failure == null) {
                try {
                    writer.write(test.testId, observation);
                } catch (Exception | AssertionError error) {
                    failure = error;
                }
            }
            receipts.put(test.testId, new Receipt(test.testId, failure, observation));
        }
        if (session != null) {
            try {
                adapter.close(session);
            } catch (Exception | AssertionError error) {
                // A leaked live session is failed cleanup, never successful
                // batch completion. Existing case evidence remains intact.
                for (Case<S> test : cases) {
                    Receipt prior = receipts.get(test.testId);
                    Throwable failure = prior.failure;
                    if (failure == null) failure = error;
                    else if (failure != error) failure.addSuppressed(error);
                    receipts.put(test.testId, new Receipt(test.testId, failure, prior.observation));
                }
            }
        }
        return Collections.unmodifiableMap(new LinkedHashMap<>(receipts));
    }

    /** Each exact JUnit test method calls this for its own case receipt. */
    public synchronized Receipt requirePassed(String testId) {
        boolean declared = cases.stream().anyMatch(test -> test.testId.equals(testId));
        if (!declared) throw new AssertionError("undeclared required case: " + testId);
        Receipt receipt = run().get(testId);
        if (receipt == null) throw new AssertionError("required case did not execute: " + testId);
        if (!receipt.passed) throw new AssertionError("required case failed: " + testId, receipt.failure);
        return receipt;
    }
}
'''


def target_session_support_files():
    """Files published by the host and mounted as read-only sandbox support."""
    return {'modport/harness/SharedGameSession.java': _SHARED_GAME_SESSION}
