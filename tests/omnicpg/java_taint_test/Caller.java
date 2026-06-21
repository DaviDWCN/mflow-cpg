public class Caller {
    void main() {
        Callee c = new Callee();
        String x = "taint";
        String y = c.doSomething(x);
    }
}
